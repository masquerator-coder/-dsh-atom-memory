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
    memoryMdTokens: 500,
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
      summaries: [{ text: '名字: 小强哥' }],
      token_count: 20,
    })

    const result = (await recall.execute({ query: '升级' }, execWithSession('session-XXX'))) as any
    expect(result.summaries).toEqual([{ text: '名字: 小强哥' }])
    expect(result.facts).toHaveLength(2)

    const rendered = recall.output!.render!({ query: '升级' }, result) as Array<{ text: string }>
    const text = rendered[0]!.text
    // summary first, then the drilled-in facts
    expect(text).toContain('【摘要】')
    // the knowledge BODY must reach the model (a render that only showed the
    // SPO title would hide exactly what recall exists to retrieve)
    expect(text).toContain('升级前先完整备份数据库。')
    expect(text).toContain('(lesson)')
    expect(text).toContain('[f-lesson-1]')
    // a fact without content still renders as a single SPO line
    expect(text).toContain('- [f-name-1] 用户名字: 小强哥 *(semantic)*')
  })

  it('memory_summary fetches the aggregate summary under the fallback scope', async () => {
    const { bridge, registered } = setup()
    const summary = registered.find((d) => d.name === 'memory_summary')!
    expect(summary).toBeTruthy()
    bridge.call.mockResolvedValue(
      '# 摘要 (Summary) — global\n\n## global (v3)\n\n职业: 工程师\n\n'
      + '> ⚠ 另有 1 条长文知识（SOP/few-shot）未展开正文，需要时用 memory_recall 检索，'
      + '或直接查看 fact_id: f-sop\n'
      + '> 覆盖 2 条活跃事实 · fact_id: f-job, f-sop',
    )

    const result = (await summary.execute({}, execWithSession('session-YYY'))) as any
    const [method, params] = bridge.call.mock.calls.at(-1) as [string, Record<string, unknown>]
    expect(method).toBe('summary')
    expect(params.user_id).toBe('global')
    expect(result.text).toContain('职业: 工程师')

    const rendered = summary.output!.render!({}, result) as Array<{ text: string }>
    // drill-down pointers must survive to the model
    expect(rendered[0]!.text).toContain('未展开正文')
    expect(rendered[0]!.text).toContain('fact_id: f-job, f-sop')
  })

  it('memory_stats / memory_user_md / memory_memory_md use the fallback user scope', async () => {
    const { bridge, registered } = setup()
    bridge.call.mockResolvedValue({})

    const stats = registered.find((d) => d.name === 'memory_stats')!
    await stats.execute({}, execWithSession('session-DDD'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')

    const userMd = registered.find((d) => d.name === 'memory_user_md')!
    await userMd.execute({}, execWithSession('session-DDD'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')

    const memMd = registered.find((d) => d.name === 'memory_memory_md')!
    await memMd.execute({}, execWithSession('session-DDD'))
    expect(bridge.call.mock.calls.at(-1)![1]!.user_id).toBe('global')
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
    expect(params.candidates).toEqual(candidates)
    expect(params.user_id).toBe('global')
    expect(params.session_id).toBe('session-FFF')
    expect(result.candidate_id).toBe('cand-1')
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
