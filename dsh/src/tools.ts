/**
 * Explicit memory tools the model can call (design §4.3 / dsh memory surface).
 *
 * Each ``execute`` is a thin, structured delegation to the Python bridge.
 * ``memory_recall`` forwards the query and the store does retrieval; the model
 * never reasons about atomic facts itself. ``memory_add`` is LLM-first: it runs
 * the shared in-process extractor and ships typed candidates (falling back to
 * the Python rule path when the LLM path is unavailable or yields nothing), so
 * free-form content is not silently dropped by the rule engine's narrow
 * patterns.
 *
 * Tools are the only model-visible surface: their ``output.schema`` keeps what
 * a model can read structured, and ``output.render`` gives the UI a readable
 * fallback text.
 *
 * @module dsh-atom-memory/tools
 */
import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { ToolRunContext } from '@deepseek-ai/dsh-tools'
import type { PythonBridge } from './bridge.ts'
import type { ExtractFn } from './llm-extractor.ts'

/**
 * Resolve the **user** scope for a tool call.
 *
 * User scope must be stable across sessions so long-term memory is shared
 * (the write side captures under the fixed fallback scope, e.g. `global`);
 * using the current session id here would isolate every session from every
 * other one and memory would never surface in a later session. The caller
 * may still override with an explicit `user` argument.
 */
function userIdOf(exec: ToolRunContext, fallback: string): string {
  return fallback
}

/**
 * Resolve the **session** scope for a tool call (falls back to a scope).
 *
 * Used only for provenance (which session wrote the memory), never as the
 * isolation scope — user isolation is governed by {@link userIdOf}.
 */
function sessionIdOf(exec: ToolRunContext, fallback: string): string {
  const sessionId = exec.agent?.session?.id
  return sessionId !== undefined ? sessionId : fallback
}

export interface ToolDeps {
  ctx: Context
  bridge: PythonBridge
  fallbackScope: string
  maxRecalledFacts: number
  memoryMdTokens: number
  /**
   * LLM-first extractor (dsh default model). When present, ``memory_add``
   * runs extraction in-process and ships typed candidates to the Python side
   * via ``persist_candidates``; the raw ``add`` rule path is the fallback.
   * Absent means the rule engine is the only extractor (original behaviour).
   */
  extract?: ExtractFn
}

/** Register all memory tools and return their disposers. */
export function registerMemoryTools(deps: ToolDeps): (() => void)[] {
  const { ctx, bridge } = deps
  const disposers: (() => void)[] = []
  const scope = deps.fallbackScope
  const call = <T>(method: string, params: Record<string, unknown>) =>
    bridge.call<T>(method, params)

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_add',
    description: '显式记住一条用户偏好、事实、事件、流程图或经验教训。传入原始内容，系统会自行抽取为原子事实。',
    parameters: {
      content: { type: 'string', required: true, description: '要记住的原始内容' },
      user: { type: 'string', description: '可选：归属用户 id（默认当前会话）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as { candidate_id?: string; status?: string }
        return [{ type: 'text', text: `已入队记忆 ${v.status ?? ''} (${v.candidate_id ?? ''})` }]
      },
    },
    async execute(args, exec) {
      const uid = args.user ?? userIdOf(exec, scope)
      const sid = sessionIdOf(exec, scope)
      // LLM-first, exactly like the capture path: extract typed candidates in
      // the dsh process (where the model lives) and persist them. This matters
      // because the Python-side ``add`` path only runs the rule engine, which
      // silently drops free-form facts (status 'skipped') that its narrow
      // patterns do not match — e.g. the prose of a summarized note.
      if (deps.extract !== undefined) {
        try {
          const candidates = await deps.extract(args.content)
          if (candidates.length > 0) {
            const r = await call<{ candidate_id?: string }>('persist_candidates', {
              user_id: uid,
              session_id: sid,
              turn_id: 0,
              candidates,
            })
            return { candidate_id: r.candidate_id ?? '', status: 'queued' }
          }
        } catch {
          /* fall through to rule extraction */
        }
      }
      return await call('add', { user_id: uid, session_id: sid, text: args.content, turn_id: 0 })
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_recall',
    description: '检索与查询相关的持久记忆原子事实。',
    parameters: {
      query: { type: 'string', required: true, description: '要检索的记忆查询' },
      user: { type: 'string', description: '可选：归属用户 id（默认当前会话）' },
      topK: { type: 'integer', description: '返回条数上限（默认按配置）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as {
          facts?: Array<{ subject?: string; predicate?: string; object?: string }>
          summaries?: Array<{ text?: string }>
        }
        const facts = v.facts ?? []
        const summaries = (v.summaries ?? []).map(s => (s.text ?? '').trim()).filter(Boolean)
        const blocks: string[] = []
        if (summaries.length > 0) blocks.push(`【摘要】${summaries.join('；')}`)
        blocks.push(facts.length === 0 ? '（无相关记忆）' : facts
          .map(f => `- ${f.subject ?? ''}${f.predicate ?? ''}: ${f.object ?? ''}`).join('\n'))
        return [{ type: 'text', text: blocks.join('\n') }]
      },
    },
    async execute(args, exec) {
      const uid = args.user ?? userIdOf(exec, scope)
      const r = await call<any>('recall', {
        user_id: uid,
        query: args.query,
        token_budget: 4000,
        top_k: args.topK ?? deps.maxRecalledFacts,
      })
      // Carry the aggregate summary alongside the ranked facts: the Python
      // side already computes and returns it, and it is the cheap "what is
      // known overall" context that makes the drilled-in facts interpretable.
      return {
        facts: r.facts ?? [],
        summaries: r.summaries ?? [],
        token_count: r.token_count ?? 0,
      }
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_summary',
    description: '查看当前用户记忆的聚合摘要（稳定属性、偏好、工作流程、近期事件、经验教训）。适合先看摘要，再按需用 memory_recall 查明细。',
    parameters: {
      user: { type: 'string', description: '可选：归属用户 id（默认当前会话）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as { text?: string }
        return [{ type: 'text', text: v.text ?? '' }]
      },
    },
    async execute(args, exec) {
      const uid = args.user ?? userIdOf(exec, scope)
      const text = await call<string>('summary', { user_id: uid })
      return { text }
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_forget',
    description: '软删除（retract）一条记忆。',
    parameters: {
      factId: { type: 'string', description: '记忆 fact id（二选一）' },
      user: { type: 'string', description: '可选：归属用户 id（默认当前会话）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render() { return [{ type: 'text', text: '已处理该记忆' }] },
    },
    async execute(args, exec) {
      if (!args.factId) throw new Error('memory_forget requires factId')
      return await call('forget', { user_id: args.user ?? userIdOf(exec, scope), fact_id: args.factId })
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_memory_md',
    description: '渲染当前用户的 memory.md（原子事实清单，含 fact_id）。',
    parameters: {
      user: { type: 'string', description: '可选：归属用户 id（默认当前会话）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as { text?: string }
        return [{ type: 'text', text: v.text ?? '' }]
      },
    },
    async execute(args, exec) {
      const uid = args.user ?? userIdOf(exec, scope)
      const text = await call<string>('memory_md', { user_id: uid, max_tokens: deps.memoryMdTokens })
      return { text }
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_user_md',
    description: '渲染当前用户的画像卡片 markdown。',
    parameters: {
      user: { type: 'string', description: '可选：归属用户 id（默认当前会话）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) {
        const v = value as { text?: string }
        return [{ type: 'text', text: v.text ?? '' }]
      },
    },
    async execute(args, exec) {
      const uid = args.user ?? userIdOf(exec, scope)
      const text = await call<string>('user_md', { user_id: uid })
      return { text }
    },
  })))

  disposers.push(ctx.tools.register(defineTool({
    name: 'memory_stats',
    description: '返回当前用户的记忆统计计数。',
    parameters: {
      user: { type: 'string', description: '可选：归属用户 id（默认当前会话）' },
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render(_args, value) { return [{ type: 'text', text: JSON.stringify(value) }] },
    },
    async execute(args, exec) {
      return await call('stats', { user_id: args.user ?? userIdOf(exec, scope) })
    },
  })))

  return disposers
}
