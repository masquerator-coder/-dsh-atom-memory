/**
 * Explicit memory tools the model can call (design §4.3 / dsh memory surface).
 *
 * Each ``execute`` is a thin, structured delegation to the Python bridge. The
 * model never reasons about atomic facts itself — ``memory_add``/``memory_recall``
 * forward raw content and the store does extraction/retrieval downstream.
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

/** Resolve the owning user/session for a tool call (falls back to a scope). */
function scopeOf(exec: ToolRunContext, fallback: string): string {
  const sessionId = exec.agent?.session?.id
  return sessionId !== undefined ? sessionId : fallback
}

export interface ToolDeps {
  ctx: Context
  bridge: PythonBridge
  fallbackScope: string
  maxRecalledFacts: number
  memoryMdTokens: number
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
      const uid = args.user ?? scopeOf(exec, scope)
      return await call('add', { user_id: uid, session_id: scopeOf(exec, scope), text: args.content, turn_id: 0 })
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
        const v = value as { facts?: Array<{ subject?: string; predicate?: string; object?: string }> }
        const items = v.facts ?? []
        return [{ type: 'text', text: items.length === 0 ? '（无相关记忆）' : items
          .map(f => `- ${f.subject ?? ''}${f.predicate ?? ''}: ${f.object ?? ''}`).join('\n') }]
      },
    },
    async execute(args, exec) {
      const uid = args.user ?? scopeOf(exec, scope)
      const r = await call<any>('recall', {
        user_id: uid,
        query: args.query,
        token_budget: 4000,
        top_k: args.topK ?? deps.maxRecalledFacts,
      })
      return { facts: r.facts ?? [], token_count: r.token_count ?? 0 }
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
      return await call('forget', { user_id: args.user ?? scopeOf(exec, scope), fact_id: args.factId })
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
      const uid = args.user ?? scopeOf(exec, scope)
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
      const uid = args.user ?? scopeOf(exec, scope)
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
      return await call('stats', { user_id: args.user ?? scopeOf(exec, scope) })
    },
  })))

  return disposers
}
