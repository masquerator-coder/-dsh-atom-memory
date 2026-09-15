/**
 * Python bridge — manages the long-lived `atom_memory.rpc` child process
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
  stdin: { write(chunk: string): boolean; on(event: 'error', listener: () => void): unknown }
  stdout: NodeJS.ReadableStream
  stderr: NodeJS.ReadableStream
  kill(signal?: NodeJS.Signals): boolean
  on(event: 'error', listener: (err: Error) => void): unknown
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
  /**
   * Called when a *healthy* process dies at runtime (not during an initial
   * `start()`), so the owner can restart it. A start failure does not fire this
   * — the owner already gets the rejected `start()` promise for that.
   */
  onExit?: () => void
}

interface Pending {
  resolve: (value: any) => void
  reject: (err: Error) => void
  timer: NodeJS.Timeout
}

/**
 * Environment for the Python child: the host env minus secret-bearing variables.
 *
 * The child only genuinely needs `PATH` (to locate the interpreter) plus the
 * encoding/buffering switches. It does not need the host's API tokens, and
 * leaking e.g. `DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY` into every spawned
 * bridge process widens the blast radius for suspicious values beyond dsh. This
 * denylist matches the common secret-name shapes case-insensitively; it is a
 * defensive guard, not a guarantee (a secret stored under a non-matching name
 * still passes through).
 */
const SECRET_ENV = /(^|_)(api[_-]?key|apitoken|access[_-]?token|auth[_-]?token|token|secret|password|passwd|credential|private[_-]?key)(_|$)/i

function childEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {}
  for (const [key, value] of Object.entries(process.env)) {
    if (value === undefined || SECRET_ENV.test(key)) continue
    env[key] = value
  }
  return env
}

/**
 * Spawn `python -m atom_memory.rpc` for the plugin.
 *
 * @param pythonBin - interpreter to use (defaults to `python`).
 */
export function defaultSpawn(
  pythonBin: string | undefined,
  cwd?: string,
): ProcessLike {
  const bin = pythonBin && pythonBin.length > 0 ? pythonBin : 'python'
  const child = spawn(bin, ['-m', 'atom_memory.rpc'], {
    stdio: ['pipe', 'pipe', 'pipe'],
    cwd,
    env: {
      ...childEnv(),
      PYTHONIOENCODING: 'utf-8',
      PYTHONUNBUFFERED: '1',
    },
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
  private readonly onExit?: BridgeDeps['onExit']

  private proc: ProcessLike | undefined
  private incoming!: Interface
  private outgoing!: { write(chunk: string): boolean }
  private readonly pending = new Map<string, Pending>()
  private nextId = 1
  private disposed = false
  /** True once a `start()` RPC has been acked, i.e. the child was healthy. */
  private ready = false

  constructor(deps: BridgeDeps) {
    this.deps = { timeoutMs: deps.timeoutMs ?? 30_000, ...deps }
    this.spawnProcess = deps.spawnProcess
    this.onEvent = deps.onEvent
    this.onLog = deps.onLog
    this.onExit = deps.onExit
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
    // A failed spawn (ENOENT) or a child that dies before a request is
    // served surfaces as an 'error' or 'exit' on the child. Node treats an
    // 'error' with no listener on a child stdout/stdin as fatal, so the child
    // is wired for errors before anything can write to it. Every error here is
    // channeled through the shared exit path (rejectAll), never thrown.
    this.proc.on('error', () => this.handleExit(null, null))
    this.wireStreams()
    this.proc.on('exit', (code, signal) => this.handleExit(code, signal))
    try {
      await this.call('start', startParams)
      this.ready = true
    } catch (err) {
      // A start failure must NOT dispose (dispose is irreversible and is only
      // for the plugin-unload path): it would make the owner's retry loop throw
      // `bridge is disposed` forever, permanently bricking memory after one
      // transient failure (e.g. a slow Python import over the RPC timeout).
      // Instead, tear down the dead child so a fresh `start()` can retry.
      await this.reset()
      throw err
    }
  }

  /**
   * Tear down the child and in-flight requests so a fresh `start()` can respawn,
   * WITHOUT setting `disposed` (which is reserved for the irreversible plugin
   * unload in `dispose()`). Used by the start-failure path.
   */
  private async reset(): Promise<void> {
    this.ready = false
    const proc = this.proc
    this.proc = undefined
    if (proc !== undefined) {
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
    this.rejectAll(new Error('bridge reset'))
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
    this.ready = false
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
    // Node kills the process on an unhandled 'error' on any child stream.
    // Writes to a dead child's stdin surface as EPIPE; the child's stdout can
    // error too. All of these are expected outcomes of a process that exited
    // and are routed to the shared exit path (handleExit -> rejectAll), never
    // thrown into the host process.
    const quiet = (): void => { /* child stream error; exit path owns teardown */ }
    proc.stdin.on('error', quiet)
    proc.stdout.on('error', quiet)
    proc.stderr.on('error', quiet)
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
    // Only a process that had successfully started is a "runtime" exit the
    // owner should restart. A spawn/start failure fires this too while `ready`
    // is false; the owner already handles that via the rejected `start()`.
    const wasReady = this.ready
    this.ready = false
    const proc = this.proc
    this.proc = undefined
    this.onReadyClose()
    if (proc !== undefined) {
      this.onLog?.(`[atom-memory] python bridge exited (code=${code}, signal=${signal})`)
    }
    this.rejectAll(new Error(`python bridge exited (code=${code}, signal=${signal})`))
    if (wasReady) this.onExit?.()
  }
}

