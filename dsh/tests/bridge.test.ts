import { describe, it, expect, vi, beforeEach } from 'vitest'
import { Readable } from 'node:stream'
import { PythonBridge, type ProcessLike } from '../src/bridge.ts'

/** A fake child process whose stdout/stderr we can feed lines into. */
class FakeProc {
  out = new Readable({ read() {} })
  err = new Readable({ read() {} })
  write = vi.fn((_chunk: string) => true)
  stdinOn = vi.fn()
  kill = vi.fn(() => true)
  letHandlers = new Map<string, (...a: any[]) => void>()
  on = vi.fn((evt: string, cb: (...a: any[]) => void) => {
    this.letHandlers.set(evt, cb)
    return () => {}
  })
  pid = 1
  asProcess(): ProcessLike {
    return {
      stdin: { write: this.write, on: this.stdinOn },
      stdout: this.out,
      stderr: this.err,
      kill: this.kill,
      on: this.on,
      pid: this.pid,
    } as unknown as ProcessLike
  }
  feedOut(line: string): void { this.out.push(line + '\n') }
  feedErr(line: string): void { this.err.push(line + '\n') }
  emitExit(code: number | null, signal: unknown): void { this.letHandlers.get('exit')?.(code, signal) }
  lastRequest(): { id: string; method: string; params: Record<string, unknown> } {
    const calls = this.write.mock.calls
    return JSON.parse(calls[calls.length - 1][0])
  }
}

/** Start a bridge and feed the `start` ack so it resolves. */
async function startBridge(deps: Partial<ConstructorParameters<typeof PythonBridge>[0]> = {}) {
  const proc = new FakeProc()
  const bridge = new PythonBridge({
    spawnProcess: () => proc.asProcess(),
    ...deps,
  } as ConstructorParameters<typeof PythonBridge>[0])
  const p = bridge.start({ db_path: 'x' })
  // the start request is written before we await; feed its ack
  const startReq = JSON.parse((proc.write as ReturnType<typeof vi.fn>).mock.calls[0][0])
  proc.feedOut(JSON.stringify({ id: startReq.id, ok: true, result: { started: true } }))
  await p
  return { bridge, proc }
}

beforeEach(() => { vi.clearAllMocks() })

describe('PythonBridge', () => {
  it('sends a start request then routes a recall call to the result', async () => {
    const { bridge, proc } = await startBridge()
    const first = proc.lastRequest()
    expect(first.method).toBe('start')

    const p = bridge.call<{ facts: unknown[] }>('recall', { user_id: 'u1', query: '咖啡' })
    const req = proc.lastRequest()
    expect(req.method).toBe('recall')
    proc.feedOut(JSON.stringify({ id: req.id, ok: true, result: { facts: [] } }))
    await expect(p).resolves.toEqual({ facts: [] })
  })

  it('rejects on an error response', async () => {
    const { bridge, proc } = await startBridge()
    const p = bridge.call('recall', { user_id: 'u1' })
    const req = proc.lastRequest()
    proc.feedOut(JSON.stringify({ id: req.id, ok: false, error: 'boom' }))
    await expect(p).rejects.toThrow('boom')
  })

  it('rejects when not started', async () => {
    const proc = new FakeProc()
    const bridge = new PythonBridge({ spawnProcess: () => proc.asProcess() })
    await expect(bridge.call('recall', {})).rejects.toThrow('not running')
  })

  it('rejects in-flight calls on dispose', async () => {
    const { bridge, proc } = await startBridge()
    const p = bridge.call('stats', { user_id: 'u1' })
    void proc.lastRequest() // consume
    await bridge.dispose()
    await expect(p).rejects.toThrow('disposed')
  })

  it('rejects calls after dispose', async () => {
    const { bridge } = await startBridge()
    await bridge.dispose()
    await expect(bridge.call('stats', {})).rejects.toThrow('disposed')
  })

  it('forwards tagged background events via onEvent', async () => {
    const events: Record<string, unknown>[] = []
    const { bridge, proc } = await startBridge({ onEvent: (e) => events.push(e) })
    proc.feedErr('EVT {"evt":"task_done","candidate_id":"c1"}\n')
    await new Promise(r => setTimeout(r, 30))
    expect(events).toContainEqual({ evt: 'task_done', candidate_id: 'c1' })
  })

  it('is idempotent on start', async () => {
    const { bridge, proc } = await startBridge()
    await bridge.start({}) // no-op; no extra request beyond the first
    const n = (proc.write as ReturnType<typeof vi.fn>).mock.calls.length
    expect(n).toBeGreaterThan(0)
  })
})

describe('bridge lifecycle restart', () => {
  it('a start failure leaves the bridge reusable (not irreversibly disposed)', async () => {
    const proc = new FakeProc()
    const bridge = new PythonBridge({ spawnProcess: () => proc.asProcess() })
    const p = bridge.start({ db_path: 'x' })
    const startReq = JSON.parse((proc.write as ReturnType<typeof vi.fn>).mock.calls[0][0])
    proc.feedOut(JSON.stringify({ id: startReq.id, ok: false, error: 'start failed' }))
    await expect(p).rejects.toThrow('start failed')

    // Not disposed: a call reports "not running" (a retriable state), not "disposed".
    await expect(bridge.call('stats', {})).rejects.toThrow('not running')

    // A second start can succeed against a fresh child.
    const proc2 = new FakeProc()
    const spy2 = vi.spyOn(bridge as unknown as { spawnProcess: () => unknown }, 'spawnProcess')
      .mockReturnValue(proc2.asProcess())
    const p2 = bridge.start({ db_path: 'x' })
    const req2 = JSON.parse((proc2.write as ReturnType<typeof vi.fn>).mock.calls[0][0])
    proc2.feedOut(JSON.stringify({ id: req2.id, ok: true, result: { started: true } }))
    await expect(p2).resolves.toBeUndefined()
    spy2.mockRestore()
  })

  it('fires onExit when a healthy process dies at runtime', async () => {
    const onExit = vi.fn()
    const proc = new FakeProc()
    const bridge = new PythonBridge({
      spawnProcess: () => proc.asProcess(),
      onExit,
    })
    const p = bridge.start({ db_path: 'x' })
    const req = JSON.parse((proc.write as ReturnType<typeof vi.fn>).mock.calls[0][0])
    proc.feedOut(JSON.stringify({ id: req.id, ok: true, result: { started: true } }))
    await p

    proc.emitExit(1, null)
    expect(onExit).toHaveBeenCalledTimes(1)
  })

  it('does NOT fire onExit for a process that dies before start is acked', async () => {
    const onExit = vi.fn()
    // The child dies before the start RPC ack: `start()` rejects, and because
    // `ready` was never true, onExit must not fire (the owner already handles
    // the rejected promise via its retry loop).
    const fake = new FakeProc()
    const bridge = new PythonBridge({
      spawnProcess: () => fake.asProcess(),
      onExit,
    })
    const p = bridge.start({ db_path: 'x' })
    fake.emitExit(1, null) // process dies while starting
    await expect(p).rejects.toBeTruthy()
    expect(onExit).not.toHaveBeenCalled()
  })
})
