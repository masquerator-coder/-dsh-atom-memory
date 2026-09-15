/**
 * Unit tests for the Host Remote controller (`src/controller.ts`).
 *
 * The controller is the seam between the browser settings panel and the Python
 * store, so the assertions here pin the *wire contract* the panel depends on:
 * which Python method is called and with which arguments. The load-bearing case
 * is `summary`: the "view memory" modal must render the text the model
 * actually receives, i.e. the compact depth (`detail: false`). Asking for the
 * default detail depth silently showed a different document than the one
 * injected into the session system prompt.
 */
import { describe, expect, it, vi } from 'vitest'
import { Context } from '@deepseek-ai/cordis'
import type { PythonBridge } from '../src/bridge.ts'
import { AtomMemoryController } from '../src/controller.ts'
import { Runtime, createRuntime } from '../src/runtime.ts'

/** A bridge double whose `call` resolves `result` and records every call. */
function fakeBridge(result: unknown = '', alive = true) {
  const call = vi.fn(async (_method: string, _params?: Record<string, unknown>) => result)
  const bridge = { alive, call } as unknown as PythonBridge
  return { bridge, call }
}

/** Build a controller with a live bridge and an enabled runtime. */
function makeController(options: { result?: unknown; alive?: boolean; enabled?: boolean } = {}) {
  const { bridge, call } = fakeBridge(options.result ?? '', options.alive ?? true)
  const runtime = new Runtime(createRuntime({ enabled: options.enabled ?? true }))
  const controller = new AtomMemoryController(new Context(), bridge, runtime)
  return { controller, call }
}

describe('AtomMemoryController.summary', () => {
  it('asks for the compact depth so the modal shows the injected snapshot', async () => {
    const { controller, call } = makeController({ result: '决策规则\n- 一条规则' })
    const text = await controller.summary({ user: 'global' })

    expect(text).toBe('决策规则\n- 一条规则')
    expect(call).toHaveBeenCalledTimes(1)
    const [method, params] = call.mock.calls[0]!
    expect(method).toBe('summary')
    expect(params).toMatchObject({ user_id: 'global', detail: false })
  })

  it('defaults the token budget to the summary budget (1500)', async () => {
    const { controller, call } = makeController()
    await controller.summary({ user: 'global' })
    expect(call.mock.calls[0]![1]).toMatchObject({ max_tokens: 1500 })
  })

  it('forwards a caller-supplied budget', async () => {
    const { controller, call } = makeController()
    await controller.summary({ user: 'global', maxTokens: 600 })
    expect(call.mock.calls[0]![1]).toMatchObject({ max_tokens: 600, detail: false })
  })

  it('unwraps a wrapped payload and tolerates an empty one', async () => {
    const wrapped = makeController({ result: { text: '# wrapped' } })
    await expect(wrapped.controller.summary({ user: 'global' })).resolves.toBe('# wrapped')

    const empty = makeController({ result: {} })
    await expect(empty.controller.summary({ user: 'global' })).resolves.toBe('')
  })

  it('refuses to render while the bridge is down', async () => {
    const { controller, call } = makeController({ alive: false })
    await expect(controller.summary({ user: 'global' })).rejects.toThrow('bridge is not running')
    expect(call).not.toHaveBeenCalled()
  })

  it('refuses to render while the plugin master switch is off', async () => {
    const { controller, call } = makeController({ enabled: false })
    await expect(controller.summary({ user: 'global' })).rejects.toThrow('memory is disabled')
    expect(call).not.toHaveBeenCalled()
  })
})

describe('AtomMemoryController fact paging', () => {
  it('applies the default page window and forwards the user scope', async () => {
    const { controller, call } = makeController({ result: { facts: [], total: 0 } })
    await controller.listFacts({ user: 'global' })
    expect(call.mock.calls[0]![1]).toMatchObject({
      user_id: 'global',
      offset: 0,
      limit: 50,
      include_retracted: false,
    })
  })

  it('rejects an edit without a fact_id before touching the bridge', async () => {
    const { controller, call } = makeController()
    await expect(controller.editFact({ user: 'global', fact_id: '' })).rejects.toThrow('fact_id')
    expect(call).not.toHaveBeenCalled()
  })
})
