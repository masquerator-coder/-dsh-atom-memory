import { describe, it, expect, vi } from 'vitest'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import { registerMemoryTools } from '../src/tools.ts'

/**
 * Regression test for the user-scope isolation bug.
 *
 * The write side (capture) persists facts under the fallback user scope
 * (`global`). Tools must query under the SAME user scope, otherwise memory
 * written in one session is invisible in another (each session has a distinct
 * id). The session id must still flow through as `session_id` for provenance.
 */

interface FakeBridge {
  call: ReturnType<typeof vi.fn>
}

function setup(extract?: (text: string) => Promise<unknown[]>) {
  const bridge: FakeBridge = {
    call: vi.fn(),
  }
  const registered: ToolDefinition[] = []
  const tools = {
    register: (def: ToolDefinition) => {
      registered.push(def)
      return () => {}
    },
  }
  const deps = {
    ctx: { tools } as any,
    bridge: bridge as any,
    fallbackScope: 'global',
    maxRecalledFacts: 10,
    summaryTokens: 500,
    extract: extract as any,
  }
  registerMemoryTools(deps)
  return { bridge, registered }
}

function execWithSession(sessionId: string | undefined) {
  return {
    agent: sessionId === undefined ? undefined : { session: { id: sessionId } },
  } as any
}

describe('memory tools user scope', () => {
  it('memory_add persists under the fallback user scope with the session id for provenance', async () => {
    const { bridge, registered } = setup()
    const add = registered.find((d) => d.name === 'memory_add')!
    expect(add).toBeTruthy()

    await add.execute({ content: '用户叫小强哥' }, execWithSession('session-AAA'))
    const [method] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('add')
    // user_id must be the stable global scope, NOT the session id.
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')
    // session id still recorded for provenance.
    expect(bridge.call.mock.calls.at(-1)![1]!.session_id).toBe('session-AAA')
  })

  it('memory_recall queries under the fallback user scope regardless of session', async () => {
    const { bridge, registered } = setup()
    const recall = registered.find((d) => d.name === 'memory_recall')!
    bridge.call.mockResolvedValue({ facts: [] })

    await recall.execute({ query: '我是谁' }, execWithSession('session-BBB'))
    const [, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(params.user_id).toBe('global')

    // A second session must still resolve to the same global user scope —
    // this is the exact bug: per-session user_id made cross-session recall empty.
    await recall.execute({ query: '我是谁' }, execWithSession('session-CCC'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')
  })

  it('memory_recall renders the full detail: fact_id, type and knowledge body', async () => {
    const { bridge, registered } = setup()
    const recall = registered.find((d) => d.name === 'memory_recall')!
    bridge.call.mockResolvedValue({
      facts: [
        {
          fact_id: 'f-lesson-1', subject: '用户', predicate: '教训',
          object: '先备份再升级', type: 'lesson',
          content: '升级前先完整备份数据库。',
        },
        { fact_id: 'f-name-1', subject: '用户', predicate: '名字', object: '小强哥', type: 'semantic', content: null },
      ],
      token_count: 20,
    })

    const result = (await recall.execute({ query: '升级' }, execWithSession('session-XXX'))) as any
    expect(result.facts).toHaveLength(2)

    const rendered = recall.output!.render!({ query: '升级' }, result) as Array<{ text: string }>
    const text = rendered[0]!.text
    // the knowledge BODY must reach the model (a render that only showed the
    // SPO title would hide exactly what recall exists to retrieve)
    expect(text).toContain('升级前先完整备份数据库。')
    expect(text).toContain('(lesson)')
    expect(text).toContain('[f-lesson-1]')
    // a fact without content still renders as a single SPO line
    expect(text).toContain('- [f-name-1] 用户名字: 小强哥 *(semantic)*')
  })

  it('memory_summary fetches the injected compact summary under the fallback scope', async () => {
    const { bridge, registered } = setup()
    const summary = registered.find((d) => d.name === 'memory_summary')!
    expect(summary).toBeTruthy()
    bridge.call.mockResolvedValue('决策规则\n- 一条规则\n\n属性: 工程师')

    const result = (await summary.execute({}, execWithSession('session-YYY'))) as any
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('summary')
    expect(params.user_id).toBe('global')
    // The injected summary is the compact depth (no fact_ids).
    expect(params.detail).toBe(false)
    expect(result.text).toContain('属性: 工程师')

    const rendered = summary.output!.render!({}, result) as Array<{ text: string }>
    expect(rendered[0]!.text).toContain('决策规则')
  })

  it('memory_stats / memory_user_md / memory_summary_detail use the fallback user scope', async () => {
    const { bridge, registered } = setup()
    bridge.call.mockResolvedValue({})

    const stats = registered.find((d) => d.name === 'memory_stats')!
    await stats.execute({}, execWithSession('session-DDD'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')

    const userMd = registered.find((d) => d.name === 'memory_user_md')!
    await userMd.execute({}, execWithSession('session-DDD'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')

    const detail = registered.find((d) => d.name === 'memory_summary_detail')!
    await detail.execute({}, execWithSession('session-DDD'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')
  })

  it('memory_summary_detail asks for the detail depth, not the injected compact one', async () => {
    const { bridge, registered } = setup()
    bridge.call.mockResolvedValue('')

    const detail = registered.find((d) => d.name === 'memory_summary_detail')!
    await detail.execute({}, execWithSession('session-DDD-2'))

    // The tool/settings view keeps fact_id references; only the frozen prompt
    // snapshot renders the compact digest.
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('summary')
    expect(params.detail).toBe(true)
  })

  it('an explicit user argument overrides the fallback scope', async () => {
    const { bridge, registered } = setup()
    const recall = registered.find((d) => d.name === 'memory_recall')!
    bridge.call.mockResolvedValue({ facts: [] })

    await recall.execute({ query: 'x', user: 'alice' }, execWithSession('session-EEE'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('alice')
  })
})

describe('memory_add LLM-first extraction', () => {
  const candidates = [
    { subject: '用户', predicate: '决策', object: '取消关键词门控', type: 'decision_rule' },
  ]

  it('persists typed candidates via persist_candidates when the LLM path yields them', async () => {
    const extract = vi.fn(async () => candidates)
    const { bridge, registered } = setup(extract)
    bridge.call.mockResolvedValue({ candidate_id: 'cand-1' })

    const add = registered.find((d) => d.name === 'memory_add')!
    const result = (await add.execute({ content: '任意自由文本' }, execWithSession('session-FFF'))) as any

    expect(extract).toHaveBeenCalledWith('任意自由文本')
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    // Must NOT go through the rules-only `add` path (which drops free-form text).
    expect(method).toBe('persist_candidates')
    // An explicit remember is the strongest durability signal, so the tool sets
    // an importance floor: without a signal every fact ties and the ordered
    // memory view degenerates to recency.
    expect(params.candidates).toEqual([
      { ...candidates[0], importance: 0.9, confidence: 0.9 },
    ])
    expect(params.user_id).toBe('global')
    expect(params.session_id).toBe('session-FFF')
    expect(result.candidate_id).toBe('cand-1')
  })

  it('never lowers an importance the extractor already ranked higher', async () => {
    const extract = vi.fn(async () => [
      { subject: '用户', predicate: '决定', object: '关键规则', importance: 0.95 },
    ])
    const { bridge, registered } = setup(extract)
    bridge.call.mockResolvedValue({ candidate_id: 'cand-hi' })

    const add = registered.find((d) => d.name === 'memory_add')!
    await add.execute({ content: '关键规则' }, execWithSession('session-HHH2'))

    const [, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect((params.candidates as any[])[0]!.importance).toBe(0.95)
  })

  it('falls back to the rule path when the LLM returns no candidates', async () => {
    const extract = vi.fn(async () => [])
    const { bridge, registered } = setup(extract)
    bridge.call.mockResolvedValue({ candidate_id: 'cand-2' })

    const add = registered.find((d) => d.name === 'memory_add')!
    await add.execute({ content: '我的发布流程是首先构建然后部署' }, execWithSession('session-GGG'))

    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('add')
    expect(params.text).toBe('我的发布流程是首先构建然后部署')
  })

  it('falls back to the rule path when the LLM path throws', async () => {
    const extract = vi.fn(async () => { throw new Error('provider down') })
    const { bridge, registered } = setup(extract)
    bridge.call.mockResolvedValue({ candidate_id: 'cand-3' })

    const add = registered.find((d) => d.name === 'memory_add')!
    await add.execute({ content: '用户喜欢黑咖啡' }, execWithSession('session-HHH'))

    const [method] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('add')
  })
})

describe('memory_add raw knowledge fallback', () => {
  // Long enough to count as substantial content (>= RAW_KNOWLEDGE_MIN_CHARS).
  const longBody = '运维手册\n' + '部署前先备份数据库，再执行迁移脚本。'.repeat(10)

  it('stores substantial content verbatim when extraction yields nothing', async () => {
    const { bridge, registered } = setup(vi.fn(async () => []))
    bridge.call.mockResolvedValue({ candidate_id: 'cand-raw' })

    const add = registered.find((d) => d.name === 'memory_add')!
    const result = (await add.execute({ content: longBody }, execWithSession('session-III'))) as any

    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    // Must NOT fall through to the rules-only `add`, which drops free-form text.
    expect(method).toBe('persist_candidates')
    const candidate = (params.candidates as any[])[0]!
    expect(candidate.type).toBe('sop')              // long-form bucket
    expect(candidate.content).toBe(longBody.trim()) // full body preserved
    expect(candidate.object).toBe('运维手册')        // title from the first line
    expect(result.fallback).toBe('raw')
  })

  it('also rescues substantial content when the extraction call throws', async () => {
    const { bridge, registered } = setup(vi.fn(async () => { throw new Error('truncated') }))
    bridge.call.mockResolvedValue({ candidate_id: 'cand-raw2' })

    const add = registered.find((d) => d.name === 'memory_add')!
    await add.execute({ content: longBody }, execWithSession('session-JJJ'))

    const [method] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('persist_candidates')
  })

  it('leaves short utterances on the rule path', async () => {
    const { bridge, registered } = setup(vi.fn(async () => []))
    bridge.call.mockResolvedValue({ candidate_id: 'cand-short' })

    const add = registered.find((d) => d.name === 'memory_add')!
    await add.execute({ content: '用户喜欢黑咖啡' }, execWithSession('session-KKK'))

    const [method] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('add')
  })
})
