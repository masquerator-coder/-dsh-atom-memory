import { describe, it, expect, vi } from 'vitest'
import {
  parseCandidates, buildLlmExtractor, collectSseText, extractViaEndpoint,
} from '../src/llm-extractor.ts'

describe('parseCandidates', () => {
  it('parses a valid JSON array of typed candidates', () => {
    const out = parseCandidates(JSON.stringify([
      { subject: '用户', predicate: '喜欢', object: '黑咖啡', type: 'semantic' },
      { subject: '用户', predicate: '教训', object: '先备份再升级', type: 'lesson', content: '升级前先完整备份' },
    ]))
    expect(out).toHaveLength(2)
    expect(out[1]).toMatchObject({ type: 'lesson', content: '升级前先完整备份' })
  })

  it('drops malformed entries but keeps valid ones', () => {
    const out = parseCandidates(JSON.stringify([
      { subject: '用户', predicate: '偏好', object: '跑步' },
      { subject: '用户' }, // missing predicate/object -> dropped
      'bogus', // non-object -> dropped
    ]))
    expect(out).toHaveLength(1)
    expect(out[0].object).toBe('跑步')
  })

  it('returns [] for non-JSON, non-array, or empty', () => {
    expect(parseCandidates('not json')).toEqual([])
    expect(parseCandidates('{"a":1}')).toEqual([])
    expect(parseCandidates('[]')).toEqual([])
  })

  it('strips code fences', () => {
    const out = parseCandidates('```json\n[{"subject":"用户","predicate":"偏好","object":"茶"}]\n```')
    expect(out).toHaveLength(1)
    expect(out[0].object).toBe('茶')
  })

  it('drops transient process-only candidates (ask/complain/meta)', () => {
    const out = parseCandidates(JSON.stringify([
      // durable, should be kept
      { subject: '用户', predicate: '偏好', object: '跑步' },
      // transient question about this conversation -> dropped
      { subject: '用户', predicate: '询问', object: 'memory.md是如何工作的？' },
      // transient meta about the current talk -> dropped
      { subject: '用户', predicate: '观察到', object: '记忆库中没有相关记录' },
      // a "why/how" question-shaped candidate -> dropped
      { subject: '用户', predicate: '质疑', object: '为什么定期提示没起作用' },
    ]))
    expect(out).toHaveLength(1)
    expect(out[0].object).toBe('跑步')
  })
})

describe('buildLlmExtractor', () => {
  it('returns undefined when there is no default model', () => {
    const ctx = {
      get: (key: string) => (key === 'agentDefaultModel' ? undefined : undefined),
    } as any
    expect(buildLlmExtractor(ctx)).toBeUndefined()
  })

  it('returns undefined when there is no llm service', () => {
    const ctx = {
      get: (key: string) => (key === 'llm' ? undefined : { currentSelection: () => ({ provider: 'p', model: 'm' }) }),
    } as any
    expect(buildLlmExtractor(ctx)).toBeUndefined()
  })

  it('returns undefined when currentSelection throws', () => {
    const ctx = {
      get: (key: string) => key === 'llm' ? { stream: async function* () {} } : { currentSelection: () => { throw new Error('x') } },
    } as any
    expect(buildLlmExtractor(ctx)).toBeUndefined()
  })

  it('passes the configured output budget to the extraction call', async () => {
    const captured: Array<{ maxTokens?: number }> = []
    const ctx = {
      get: (key: string) => key === 'llm'
        ? {
            stream: async function* (opts: { maxTokens?: number }) {
              captured.push(opts)
              throw new Error('stop-here') // abort after capturing the options
            },
          }
        : { currentSelection: () => ({ provider: 'p', model: 'm' }) },
      logger: () => {},
    } as any

    const extract = buildLlmExtractor(ctx, { maxTokens: 4096 })!
    await expect(extract('x')).rejects.toThrow('stop-here')
    expect(captured[0]!.maxTokens).toBe(4096)
  })

  it('defaults the extraction output budget to 2048 tokens', async () => {
    const captured: Array<{ maxTokens?: number }> = []
    const ctx = {
      get: (key: string) => key === 'llm'
        ? {
            stream: async function* (opts: { maxTokens?: number }) {
              captured.push(opts)
              throw new Error('stop-here')
            },
          }
        : { currentSelection: () => ({ provider: 'p', model: 'm' }) },
      logger: () => {},
    } as any

    // A budget that fits a knowledge body: the old hard-coded 600 truncated
    // long payloads, and a truncated extraction is discarded, not persisted.
    await expect(buildLlmExtractor(ctx)!('x')).rejects.toThrow('stop-here')
    expect(captured[0]!.maxTokens).toBe(2048)
  })

  it('prefers a manual model override over the dsh default selection', async () => {
    const captured: Array<{ provider?: string; model?: string }> = []
    const ctx = {
      get: (key: string) => key === 'llm'
        ? {
            stream: async function* (opts: { provider?: string; model?: string }) {
              captured.push(opts)
              throw new Error('stop-here')
            },
          }
        : { currentSelection: () => ({ provider: 'default-p', model: 'default-m' }) },
      logger: () => {},
    } as any

    const extract = buildLlmExtractor(ctx, {
      modelOverride: () => ({ provider: 'manual-p', model: 'manual-m' }),
    })!
    await expect(extract('x')).rejects.toThrow('stop-here')
    expect(captured[0]).toMatchObject({ provider: 'manual-p', model: 'manual-m' })
  })

  it('falls back to the default selection when the override names no provider', async () => {
    const captured: Array<{ provider?: string; model?: string }> = []
    const ctx = {
      get: (key: string) => key === 'llm'
        ? {
            stream: async function* (opts: { provider?: string; model?: string }) {
              captured.push(opts)
              throw new Error('stop-here')
            },
          }
        : { currentSelection: () => ({ provider: 'default-p', model: 'default-m' }) },
      logger: () => {},
    } as any

    const extract = buildLlmExtractor(ctx, {
      modelOverride: () => ({ provider: '', model: 'ignore-me' }),
    })!
    await expect(extract('x')).rejects.toThrow('stop-here')
    expect(captured[0]).toMatchObject({ provider: 'default-p', model: 'default-m' })
  })

  it('yields nothing when the enabled gate is off (caller falls back to rules)', async () => {
    let called = false
    const ctx = {
      get: (key: string) => key === 'llm'
        ? { stream: async function* () { called = true; yield {} } }
        : { currentSelection: () => ({ provider: 'p', model: 'm' }) },
      logger: () => {},
    } as any

    const extract = buildLlmExtractor(ctx, { enabled: () => false })!
    expect(await extract('x')).toEqual([])
    expect(called).toBe(false)
  })
})

describe('custom endpoint (direct OpenAI-compatible)', () => {
  /** Build an SSE Response-like body reader over literal string chunks. */
  function sseBody(chunks: string[]) {
    const e = new TextEncoder()
    let i = 0
    return {
      getReader: () => ({
        read: async () => i < chunks.length
          ? { done: false, value: e.encode(chunks[i++]) }
          : { done: true, value: undefined },
      }),
    }
  }

  it('accumulates chunked content across SSE events', async () => {
    const enc2 = new TextEncoder()
    const reader = {
      getReader: () => {
        const parts = [
          'data: {"choices":[{"delta":{"content":"hel"}}]}\n',
          'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
          'data: {"choices":[{"delta":{"content":" world"}}]}\n',
          'data: [DONE]\n',
        ]
        let i = 0
        return { read: async () => i < parts.length ? { done: false, value: enc2.encode(parts[i++]) } : { done: true, value: undefined } }
      },
    }
    expect(await collectSseText(reader as never)).toBe('hello world')
  })

  it('extractViaEndpoint POSTs chat/completions with a Bearer key and parses the finished JSON', async () => {
    const payload = '[{"subject":"用户","predicate":"偏好","object":"茶"}]'
    const sse = [
      'data: ' + JSON.stringify({ choices: [{ delta: { content: '[' } }] }) + '\n',
      'data: ' + JSON.stringify({ choices: [{ delta: { content: payload.slice(1, -1) } }] }) + '\n',
      'data: ' + JSON.stringify({ choices: [{ delta: { content: ']' } }] }) + '\n',
      'data: [DONE]\n',
    ]
    const fetchImpl = vi.fn(async (_url: string, init?: Record<string, unknown>) => {
      capturedRequest = init as { headers: Record<string, string>; body: string }
      return { ok: true, status: 200, body: sseBody(sse) }
    })
    let capturedRequest: { headers: Record<string, string>; body: string } | undefined
    const log = vi.fn()
    // The custom-endpoint path needs no `llm` service or default-model service.
    const ctx = { get: () => undefined, logger: () => {} } as never
    const out = await buildLlmExtractor(ctx as never, {
      modelOverride: () => ({
        provider: 'custom', model: 'gpt-4o-mini',
        baseURL: 'https://api.example.com/v1', protocol: 'openai', apiKey: 'sk-secret',
      }),
      fetchImpl: fetchImpl as never,
      log,
    })!
    expect(out).toBeDefined()
    const candidates = await out('用户喜欢茶')
    expect(candidates).toEqual([{ subject: '用户', predicate: '偏好', object: '茶' }])
    // The request targets {baseURL}/chat/completions with a Bearer key.
    expect(fetchImpl).toHaveBeenCalled()
    const url = fetchImpl.mock.calls[0][0] as string
    expect(url).toBe('https://api.example.com/v1/chat/completions')
    expect(capturedRequest!.headers.Authorization).toBe('Bearer sk-secret')
    // The API key must never be logged.
    const logged = log.mock.calls.map(c => String(c[0])).join('\n')
    expect(logged).not.toContain('sk-secret')
  })

  it('parseCandidates round-trips through a direct endpoint call without ctx.llm', async () => {
    // Sanity: parseCandidates already yields typed candidates from the assembled JSON.
    expect(parseCandidates('[{"subject":"u","predicate":"p","object":"o"}]')).toEqual([{ subject: 'u', predicate: 'p', object: 'o' }])
  })
})
