import { describe, it, expect, vi } from 'vitest'
import { registerCapture } from '../src/capture.ts'

interface FakeSessionEvent {
  type: string
  data: { content?: Array<{ type?: string; text?: string }>; source?: { kind?: string } }
  seq?: number
}

function makeCtx() {
  const byEvent = new Map<string, Array<(...args: any[]) => unknown>>()
  const on = vi.fn((event: string, handler: (...args: any[]) => unknown) => {
    byEvent.set(event, [...(byEvent.get(event) ?? []), handler])
    return () => { /* no-op disposer */ }
  })
  return {
    ctx: { on } as any,
    on,
    handlersOf: (event: string) => byEvent.get(event) ?? [],
  }
}

/** Feed a ``user/message`` durable event to a registered capture hook. */
function send(session: string, text: string, byEvent: ReturnType<typeof makeCtx>['handlersOf']) {
  const handler = byEvent('session/event')[0] as (s: unknown, e: FakeSessionEvent) => void
  handler({ id: session }, {
    type: 'user/message',
    data: { content: [{ type: 'text', text }], source: { kind: 'user' } },
    seq: 1,
  })
}

describe('registerCapture', () => {
  it('fires capture for any direct user message (no keyword gate)', async () => {
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => {})
    registerCapture(
      { ctx, capture },
      { captureEnabled: true, preCompressionCapture: false, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    expect(ctx.on).toHaveBeenCalledWith('session/event', expect.any(Function))
    // No strong-fact keyword present, but capture must still fire.
    send('s1', '总结我的obsidian工作笔记', handlersOf)
    await new Promise(r => setTimeout(r, 10))
    expect(capture).toHaveBeenCalledWith('总结我的obsidian工作笔记', 's1')
  })

  it('does not fire for plugin-sourced content', async () => {
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => {})
    registerCapture(
      { ctx, capture },
      { captureEnabled: true, preCompressionCapture: false, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    const handler = handlersOf('session/event')[0] as (s: unknown, e: FakeSessionEvent) => void
    handler({ id: 's1' }, {
      type: 'user/message',
      data: { content: [{ type: 'text', text: '用户喜欢黑咖啡' }], source: { kind: 'plugin' } },
      seq: 1,
    })
    await new Promise(r => setTimeout(r, 10))
    expect(capture).not.toHaveBeenCalled()
  })

  it('does not fire for non-user-message events', async () => {
    const { ctx, handlersOf } = makeCtx()
    const capture = vi.fn(async () => {})
    registerCapture(
      { ctx, capture },
      { captureEnabled: true, preCompressionCapture: false, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    const handler = handlersOf('session/event')[0] as (s: unknown, e: FakeSessionEvent) => void
    handler({ id: 's1' }, {
      type: 'turn/end',
      data: { content: [{ type: 'text', text: '用户喜欢黑咖啡' }], source: { kind: 'user' } },
      seq: 2,
    })
    await new Promise(r => setTimeout(r, 10))
    expect(capture).not.toHaveBeenCalled()
  })

  it('does not register capture when disabled', () => {
    const { ctx } = makeCtx()
    registerCapture(
      { ctx, capture: vi.fn(async () => {}) },
      { captureEnabled: false, preCompressionCapture: false, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    expect(ctx.on).not.toHaveBeenCalled()
  })

  it('rescues a message whose immediate capture failed via pre-compression', async () => {
    const { ctx, handlersOf } = makeCtx()
    // First attempt fails (bridge down); the rescue retry succeeds.
    const outcomes: Array<'fail' | 'ok'> = ['fail', 'ok']
    const capture = vi.fn(async () => {
      const next = outcomes.shift()
      if (next === 'fail') throw new Error('bridge down')
    })
    registerCapture(
      { ctx, capture },
      { captureEnabled: true, preCompressionCapture: true, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )

    send('s1', '用户偏好黑咖啡', handlersOf)
    await new Promise(r => setTimeout(r, 10))
    expect(capture).toHaveBeenCalledTimes(1)
    expect(capture).toHaveBeenCalledWith('用户偏好黑咖啡', 's1')

    // Trigger the pre-compression rescue hook (llm/stream with purpose
    // 'compaction'). It is a generator waterfall; drain it to run the sweep.
    const stream = handlersOf('llm/stream')[0] as
      (options: { purpose?: string; sessionId?: string }, next: () => AsyncGenerator<string>) => AsyncGenerator<string>
    const results: string[] = []
    async function* next() { yield 'ORIGINAL' }
    for await (const chunk of stream(
      { purpose: 'compaction', sessionId: 's1' },
      next as unknown as typeof next,
    )) {
      results.push(chunk)
    }

    // The failed message was rescued: capture now ran twice (initial + rescue)
    // with the same text and session, and the original stream still flows.
    expect(capture).toHaveBeenCalledTimes(2)
    expect(capture.mock.calls[1]).toEqual(['用户偏好黑咖啡', 's1'])
    expect(results).toEqual(['ORIGINAL'])
  })
})
