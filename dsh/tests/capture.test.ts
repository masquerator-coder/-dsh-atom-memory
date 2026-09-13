import { describe, it, expect, vi } from 'vitest'
import { registerCapture, hasSignal } from '../src/capture.ts'

interface FakeSessionEvent {
  type: string
  data: { content?: Array<{ type?: string; text?: string }>; source?: { kind?: string } }
  seq?: number
}

function makeCtx() {
  const handlers: Array<(session: unknown, event: FakeSessionEvent) => void> = []
  const on = vi.fn((_event: string, handler: (s: unknown, e: FakeSessionEvent) => void) => {
    handlers.push(handler)
    return () => { /* no-op disposer */ }
  })
  return { ctx: { on } as any, handlers, on }
}

describe('hasSignal', () => {
  it('detects strong-fact keywords', () => {
    expect(hasSignal('用户喜欢黑咖啡')).toBe(true)
    expect(hasSignal('这只是一个普通打招呼')).toBe(false)
  })
})

describe('registerCapture', () => {
  it('fires capture for a direct user message with a signal', async () => {
    const { ctx, handlers } = makeCtx()
    const capture = vi.fn(async () => {})
    registerCapture(
      { ctx, capture },
      { captureEnabled: true, preCompressionCapture: false, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    expect(ctx.on).toHaveBeenCalledWith('session/event', expect.any(Function))
    const handler = handlers[0]
    handler({ id: 's1' }, {
      type: 'user/message',
      data: { content: [{ type: 'text', text: '用户喜欢黑咖啡' }], source: { kind: 'user' } },
      seq: 1,
    })
    await new Promise(r => setTimeout(r, 10))
    expect(capture).toHaveBeenCalledWith('用户喜欢黑咖啡', 's1')
  })

  it('does not fire for plugin-sourced content', async () => {
    const { ctx, handlers } = makeCtx()
    const capture = vi.fn(async () => {})
    registerCapture(
      { ctx, capture },
      { captureEnabled: true, preCompressionCapture: false, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    handlers[0]({ id: 's1' }, {
      type: 'user/message',
      data: { content: [{ type: 'text', text: '用户喜欢黑咖啡' }], source: { kind: 'plugin' } },
      seq: 1,
    })
    await new Promise(r => setTimeout(r, 10))
    expect(capture).not.toHaveBeenCalled()
  })

  it('does not fire for non-user-message events', async () => {
    const { ctx, handlers } = makeCtx()
    const capture = vi.fn(async () => {})
    registerCapture(
      { ctx, capture },
      { captureEnabled: true, preCompressionCapture: false, nudgeEnabled: false, nudgeIntervalMs: 60_000 },
    )
    handlers[0]({ id: 's1' }, {
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
})
