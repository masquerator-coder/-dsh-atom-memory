/**
 * Python bridge — manages the long-lived `dsh_atom_memory.rpc` child process
 * and speaks the NDJSON stdio protocol with it.
 *
 * The bridge owns zero model-visible state: it is a pure request/response
 * transport plus a best-effort background-event tap. It never synthesises
 * content a model could see; every fact is persisted and later recalled by the
 * Python side, and every request/response here is idempotent over the wire.
 *
 * Design (see repo design doc, "bridging"):
 *  - stdin: one NDJSON request per line `{"id","method","params"}`.
 *  - stdout: one NDJSON response per line `{"id","ok","result"|"error"}`.
 *  - stderr: tagged background events (`EVT …`) and logs (`LOG …`), filtered.
 *
 * Process lifecycle is tied to the owning plugin: `start()` spawns on demand,
 * `dispose()` kills the child when the plugin unloads, and every in-flight
 * request is rejected on process death so callers never hang.
 *
 * @module dsh-atom-memory/bridge
 */
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process'
import { createInterface, type Interface } from 'node:readline'
import { randomUUID } from 'node:crypto'

/** Shape of the process we drive — injectable so tests can fake it. */
export interface ProcessLike {
  stdin: { write(chunk: string): boolean }
  stdout: NodeJS.ReadableStream
  stderr: NodeJS.ReadableStream
  kill(signal?: NodeJS.Signals): boolean
  on(event: 'exit', listener: (code: number | null, signal: NodeJS.Signals | null) => void): unknown
  pid?: number
}

export interface BridgeDeps {
  /** Spawn the Python child process (injectable for tests). */
  spawnProcess: () => ProcessLike
  /** Default per-request timeout in ms. */
  timeoutMs?: number
  /** Called for each background event line, e.g. to forward to logs. */
  onEvent?: (event: Record<string, unknown>) => void
  /** Called for each tagged log line from the Python process. */
  onLog?: (message: string) => void
}

interface Pending {
  resolve: (value: any) => void
  reject: (err: Error) => void
  timer: NodeJS.Timeout
}

/**
 * Spawn `python -m dsh_atom_memory.rpc` for the plugin.
 *
 * @param pythonBin - interpreter to use (defaults to `python`).
 */
export function defaultSpawn(
  pythonBin: string | undefined,
  cwd?: string,
): ProcessLike {
  const bin = pythonBin && pythonBin.length > 0 ? pythonBin : 'python'
  const child = spawn(bin, ['-m', 'dsh_atom_memory.rpc'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    cwd,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
  }) as ChildProcessWithoutNullStreams
  return child as unknown as ProcessLike
}

/**
 * A lightweight NDJSON request/response client for one bridge protocol.
 */
export class PythonBridge {
  private readonly deps: Required<Pick<BridgeDeps, 'timeoutMs'>>
  private readonly spawnProcess: BridgeDeps['spawnProcess']
  private readonly onEvent?: BridgeDeps['onEvent']
  private readonly onLog?: BridgeDeps['onLog']

  private proc: ProcessLike | undefined
  private incoming!: Interface
  private outgoing!: { write(chunk: string): boolean }
  private readonly pending = new Map<string, Pending>()
  private nextId = 1
  private disposed = false

  constructor(deps: BridgeDeps) {
    this.deps = { timeoutMs: deps.timeoutMs ?? 30_000, ...deps }
    this.spawnProcess = deps.spawnProcess
    this.onEvent = deps.onEvent
    this.onLog = deps.onLog
  }

  /** Whether a child process is currently alive. */
  get alive(): boolean {
    return this.proc !== undefined
  }

  /**
   * Send one RPC request and await its result.
   *
   * @returns the decoded `result` on success.
   * @throws if the process is not alive, the request errors, or it times out.
   */
  call<T = unknown>(method: string, params: Record<string, unknown> = {}, timeoutMs?: number): Promise<T> {
    if (this.disposed) return Promise.reject(new Error('bridge is disposed'))
    if (this.proc === undefined) return Promise.reject(new Error('bridge is not running'))
    const id = String(this.nextId++)
    const wire = JSON.stringify({ id, method, params })
    void this.outgoing.write(wire + '\n')

    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id)
        reject(new Error(`RPC ${method} timed out after ${timeoutMs ?? this.deps.timeoutMs}ms`))
      }, timeoutMs ?? this.deps.timeoutMs)
      this.pending.set(id, { resolve, reject, timer })
    })
  }

  /**
   * Start the child process and confirm it is ready (`start` RPC acked).
   */
  async start(startParams: Record<string, unknown> = {}, cwd?: string): Promise<void> {
    if (this.disposed) throw new Error('bridge is disposed')
    if (this.proc !== undefined) return // already running / starting
    this.proc = this.spawnProcess()
    this.wireStreams()
    this.proc.on('exit', (code, signal) => this.handleExit(code, signal))
    try {
      await this.call('start', startParams)
    } catch (err) {
      await this.dispose()
      throw err
    }
  }

  /** Send the Python `start`/config had already been acked lazily. */
  async health(): Promise<boolean> {
    if (this.proc === undefined) return false
    try {
      const r = await this.call<{ ok: boolean }>('health', {}, 5_000)
      return r.ok === true
    } catch {
      return false
    }
  }

  /**
   * Stop the Python memory (flushing the worker / DB) and kill the process.
   * Idempotent and safe to call from an effect disposer.
   */
  async dispose(): Promise<void> {
    if (this.disposed) return
    this.disposed = true
    const proc = this.proc
    this.proc = undefined
    if (proc !== undefined) {
      // Best-effort graceful stop so the worker flushes before exit.
      try {
        proc.stdin.write(JSON.stringify({ id: 'shutdown', method: 'stop' }) + '\n')
      } catch {
        /* the child may already be gone */
      }
      try {
        this.onReadyClose()
      } catch {
        /* ignore */
      }
      proc.kill()
    }
    this.rejectAll(new Error('bridge disposed'))
  }

  // -- internals ------------------------------------------------------------

  private wireStreams(): void {
    const proc = this.proc!
    this.incoming = createInterface({ input: proc.stdout, crlfDelay: Infinity })
    this.outgoing = proc.stdin
    this.incoming.on('line', (line) => {
      if (!line) return
      this.handleLine(line)
    })
    createInterface({ input: proc.stderr, crlfDelay: Infinity }).on('line', (line) => {
      this.handleStderr(line)
    })
  }

  private handleLine(line: string): void {
    let msg: Record<string, unknown>
    try {
      msg = JSON.parse(line)
    } catch {
      return // malformed line from the child: ignore
    }
    const id = msg.id
    if (id === undefined) return
    const pending = this.pending.get(String(id))
    if (pending === undefined) return
    clearTimeout(pending.timer)
    this.pending.delete(String(id))
    if (msg.ok === true) {
      pending.resolve(msg.result)
    } else {
      pending.reject(new Error(String(msg.error ?? 'RPC error')))
    }
  }

  private handleStderr(line: string): void {
    // Tagged frames: `EVT {json}` and `LOG {text}`.
    if (line.startsWith('EVT ')) {
      try {
        this.onEvent?.(JSON.parse(line.slice(4)))
      } catch {
        /* ignore */
      }
      return
    }
    if (line.startsWith('LOG ')) {
      this.onLog?.(line.slice(4))
      return
    }
    // Untagged noise from the child (e.g. a warning) is dropped.
  }

  private onReadyClose(): void {
    // Detach listeners so no stray 'line' events after we stop caring.
    try {
      this.incoming?.close()
    } catch {
      /* ignore */
    }
  }

  private rejectAll(err: Error): void {
    for (const p of this.pending.values()) {
      clearTimeout(p.timer)
      p.reject(err)
    }
    this.pending.clear()
  }

  private handleExit(code: number | null, signal: NodeJS.Signals | null): void {
    if (this.disposed) return
    const proc = this.proc
    this.proc = undefined
    this.onReadyClose()
    if (proc !== undefined) {
      this.onLog?.(`[atom-memory] python bridge exited (code=${code}, signal=${signal})`)
    }
    this.rejectAll(new Error(`python bridge exited (code=${code}, signal=${signal})`))
  }
}
