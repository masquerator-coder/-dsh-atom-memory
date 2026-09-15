import { describe, it, expect, vi } from 'vitest'
import { registerMemoryContext } from '../src/context.ts'

interface FakeSection { name: string; text: string }

function makeCtx() {
  const sections: Array<{ name: string; order: number; text: unknown }> = []
  const handlers: Array<[string, (...args: any[]) => any]> = []
  const ctx = {
    systemPrompt: {
      section: (s: { name: string; order: number; text: unknown }) => {
        sections.push(s)
        return () => {}
      },
      getSectionOrder: () => 2300,
    },
    on: (evt: string, fn: (...args: any[]) => any) => {
      handlers.push([evt, fn])
      return () => {}
    },
  }
  return { ctx: ctx as any, sections, handlers }
}

function assembly() {
  return {
    sections: [{ name: 'atom-memory-awareness', text: 'AWARENESS' }] as FakeSection[],
    contexts: [], tools: [], variables: {},
  } as any
}

function assembleHandler(handlers: Array<[string, (...args: any[]) => any]>) {
  const found = handlers.find(([e]) => e === 'system-prompt/assemble')
  if (!found) throw new Error('no system-prompt/assemble listener registered')
  return found[1]
}

const agentCtx = (id: string) => ({ agent: { session: { id } } }) as any
const next = (a: any) => async () => a

describe('registerMemoryContext', () => {
  it('registers the static awareness section', () => {
    const { ctx, sections } = makeCtx()
    registerMemoryContext({
      ctx, bridge: { call: vi.fn() } as any,
      userScope: 'global', resolveMaxTokens: () => 1500, snapshotEnabled: false,
    })
    expect(sections.map(s => s.name)).toEqual(['atom-memory-awareness'])
  })

  it('drops the awareness text from the system prompt when the master switch is off', () => {
    const { ctx, sections } = makeCtx()
    const isEnabled = vi.fn(() => false)
    registerMemoryContext({
      ctx, bridge: { call: vi.fn() } as any,
      userScope: 'global', resolveMaxTokens: () => 1500, snapshotEnabled: true,
      isEnabled,
    })
    const awareness = sections.find(s => s.name === 'atom-memory-awareness')!
    const text = (awareness.text as (c: unknown) => string)({} as any)
    expect(text).toBe('')
    expect(isEnabled).toHaveBeenCalled()
  })

  it('keeps the awareness text when the master switch is on (or unset)', () => {
    const { ctx, sections } = makeCtx()
    registerMemoryContext({
      ctx, bridge: { call: vi.fn() } as any,
      userScope: 'global', resolveMaxTokens: () => 1500, snapshotEnabled: true,
      isEnabled: () => true,
    })
    const awareness = sections.find(s => s.name === 'atom-memory-awareness')!
    const text = (awareness.text as (c: unknown) => string)({} as any)
    expect(text).toContain('You have persistent long-term memory')
  })

  it('injects no memory into the assembly when the master switch is off', async () => {
    const bridge = { call: vi.fn(async () => '# Memory\n- fact') }
    const { ctx, handlers } = makeCtx()
    registerMemoryContext({
      ctx, bridge: bridge as any,
      userScope: 'global', resolveMaxTokens: () => 1500, snapshotEnabled: true,
      isEnabled: () => false,
    })
    const handler = assembleHandler(handlers)
    const result = await handler(assembly(), agentCtx('s1'), next(assembly()))
    expect(result.sections.some((s: FakeSection) => s.name === 'atom-memory-snapshot')).toBe(false)
    expect(bridge.call).not.toHaveBeenCalled()
  })

  it('injects the snapshot once per session and then serves it frozen', async () => {
    const bridge = {
      call: vi.fn(async (_method: string, _params: Record<string, unknown>) => '# Memory\n- [id] 用户 — 名字: 小强哥'),
    }
    const { ctx, handlers } = makeCtx()
    registerMemoryContext({
      ctx, bridge: bridge as any,
      userScope: 'global', resolveMaxTokens: () => 1500, snapshotEnabled: true,
    })
    const handler = assembleHandler(handlers)

    const first = await handler(assembly(), agentCtx('s1'), next(assembly()))
    const text1 = first.sections.find((s: FakeSection) => s.name === 'atom-memory-snapshot')?.text
    const second = await handler(assembly(), agentCtx('s1'), next(assembly()))
    const text2 = second.sections.find((s: FakeSection) => s.name === 'atom-memory-snapshot')?.text

    expect(text1).toBeTruthy()
    expect(text2).toBe(text1)          // byte-identical => KV-stable
    expect(bridge.call).toHaveBeenCalledTimes(1) // frozen: no re-read
    expect(bridge.call.mock.calls[0]![0]).toBe('summary')
    expect(bridge.call.mock.calls[0]![1]).toMatchObject({ user_id: 'global', max_tokens: 1500 })
    // The injected snapshot must be the compact depth: it is paid for on every
    // request, so it drops the fact_id UUIDs and groups by memory type.
    expect(bridge.call.mock.calls[0]![1]).toMatchObject({ detail: false })
    // inserted directly after the awareness section
    expect(first.sections.map((s: FakeSection) => s.name)).toEqual([
      'atom-memory-awareness', 'atom-memory-snapshot',
    ])
  })

  it('reads independently for a different session', async () => {
    const bridge = { call: vi.fn(async () => '# Memory\n- fact') }
    const { ctx, handlers } = makeCtx()
    registerMemoryContext({
      ctx, bridge: bridge as any,
      userScope: 'global', resolveMaxTokens: () => 1500, snapshotEnabled: true,
    })
    const handler = assembleHandler(handlers)
    await handler(assembly(), agentCtx('s1'), next(assembly()))
    await handler(assembly(), agentCtx('s2'), next(assembly()))
    expect(bridge.call).toHaveBeenCalledTimes(2)
  })

  it('does not freeze a transient bridge failure, and freezes the retry', async () => {
    let fail = true
    const bridge = {
      call: vi.fn(async () => {
        if (fail) throw new Error('bridge not running')
        return '# Memory\n- recovered'
      }),
    }
    const { ctx, handlers } = makeCtx()
    registerMemoryContext({
      ctx, bridge: bridge as any,
      userScope: 'global', resolveMaxTokens: () => 1500, snapshotEnabled: true,
    })
    const handler = assembleHandler(handlers)

    const failed = await handler(assembly(), agentCtx('s1'), next(assembly()))
    expect(failed.sections.some((s: FakeSection) => s.name === 'atom-memory-snapshot')).toBe(false)

    fail = false
    const retried = await handler(assembly(), agentCtx('s1'), next(assembly()))
    expect(retried.sections.some((s: FakeSection) => s.name === 'atom-memory-snapshot')).toBe(true)

    // once successfully read, subsequent assemblies are frozen (no third read)
    await handler(assembly(), agentCtx('s1'), next(assembly()))
    expect(bridge.call).toHaveBeenCalledTimes(2)
  })

  it('injects nothing and reads nothing when there is no agent/session', async () => {
    const bridge = { call: vi.fn(async () => '# Memory') }
    const { ctx, handlers } = makeCtx()
    registerMemoryContext({
      ctx, bridge: bridge as any,
      userScope: 'global', resolveMaxTokens: () => 1500, snapshotEnabled: true,
    })
    const handler = assembleHandler(handlers)
    const result = await handler(assembly(), {} as any, next(assembly()))
    expect(bridge.call).not.toHaveBeenCalled()
    expect(result.sections).toHaveLength(1)
  })

  it('registers no assemble listener when snapshot injection is disabled', () => {
    const { ctx, handlers } = makeCtx()
    registerMemoryContext({
      ctx, bridge: { call: vi.fn() } as any,
      userScope: 'global', resolveMaxTokens: () => 1500, snapshotEnabled: false,
    })
    expect(handlers).toHaveLength(0)
  })

  /**
   * The injected budget is resolved at each freeze, which is what makes the
   * settings panel's "injected size" control usable: a session that has not
   * frozen yet picks up the new budget, while an already-frozen session keeps
   * serving its cached text byte-for-byte (so the prompt prefix — and the
   * provider's KV cache — is never invalidated mid-session).
   */
  it('resolves the budget at each freeze instead of at registration', async () => {
    let budget = 1500
    const bridge = {
      call: vi.fn(async (_method: string, _params: Record<string, unknown>) => '# Memory\n- fact'),
    }
    const { ctx, handlers } = makeCtx()
    registerMemoryContext({
      ctx, bridge: bridge as any,
      userScope: 'global', resolveMaxTokens: () => budget, snapshotEnabled: true,
    })
    const handler = assembleHandler(handlers)

    await handler(assembly(), agentCtx('s1'), next(assembly()))
    expect(bridge.call.mock.calls[0]![1]).toMatchObject({ max_tokens: 1500 })

    // The user shrinks the budget in the settings panel.
    budget = 300

    // The frozen session is untouched: no re-read, same text.
    const again = await handler(assembly(), agentCtx('s1'), next(assembly()))
    const text = again.sections.find((s: FakeSection) => s.name === 'atom-memory-snapshot')?.text
    expect(text).toBeTruthy()
    expect(bridge.call).toHaveBeenCalledTimes(1)

    // A session that freezes afterwards uses the new budget.
    await handler(assembly(), agentCtx('s2'), next(assembly()))
    expect(bridge.call).toHaveBeenCalledTimes(2)
    expect(bridge.call.mock.calls[1]![1]).toMatchObject({ max_tokens: 300 })
  })
})
