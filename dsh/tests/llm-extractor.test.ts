import { describe, it, expect } from 'vitest'
import { parseCandidates, buildLlmExtractor } from '../src/llm-extractor.ts'

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
})
