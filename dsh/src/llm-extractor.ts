/**
 * LLM-first extractor adapter.
 *
 * Extraction runs on the dsh side (where ``ctx.llm`` and the default model
 * live), then the resulting typed candidates are shipped to the Python memory
 * process via ``persist_candidates`` (RPC → ``persist_pre`` worker task). The
 * rule engine lives entirely in Python, so this adapter is the *first* path and
 * Python is the *fallback* — matching the library's LLM-first, rule-fallback
 * precedence across the process boundary.
 *
 * The default model is read from the dsh "current preset's first model"
 * selection via ``ctx.get('agentDefaultModel').currentSelection()``. When no
 * default model is available the adapter returns ``[]`` and the caller falls
 * back to the raw ``add`` path (pure Python rule extraction) — never a silent
 * drop.
 *
 * @module dsh-atom-memory/llm-extractor
 */
import { createUserMessage, BlockAssembler } from '@deepseek-ai/dsh-llm'
import type { GenerateOptions } from '@deepseek-ai/dsh-llm'
import type { Context } from '@deepseek-ai/cordis'

/** One typed candidate matching the Python ``persist_candidates`` wire shape. */
export interface ExtractedCandidate {
  subject: string
  predicate: string
  object: string
  type?: string
  content?: string
  qualifiers?: Record<string, unknown>
  confidence?: number
  importance?: number
}

/** Minimal structural surface of the ``llm`` service. */
export interface LlmLike {
  stream(options: GenerateOptions): AsyncIterable<unknown>
}

/** Minimal structural surface of the default-model service. */
export interface AgentDefaultModelLike {
  currentSelection(): { provider: string; model: string; reasoningEffort?: string }
}

/** The extraction callable signature the capture/tool layer uses. */
export type ExtractFn = (text: string) => Promise<ExtractedCandidate[]>

/** Fixed, deterministic extraction prompt (strict, injection-isolated). */
const EXTRACTION_SYSTEM = `You extract atomic memory facts from a user utterance.
Return ONLY a JSON array. Each element is an object with keys:
- "subject" (entity, use "用户" for the user), "predicate" (relation),
- "object" (the value), and optionally "type" and "content".
"type" is one of: semantic, procedural, episodic, sop, decision_rule, few_shot, lesson.
For knowledge facts, put the full body in "content" and a short title in "object".

CRITICAL - only extract facts that are worth remembering long-term:
- Save durable, reusable knowledge: decisions, workflows, procedures, lessons,
  preferences, stable attributes, and anything that remains valuable in future
  sessions.
- Do NOT save transient, process-only details that only matter in this single
  turn: questions asked, complaints made, meta-commentary about the current
  conversation, the fact that a task was requested, how a system was debugged,
  or the wording of instructions the user gave. These are not stable facts.
- If the utterance contains no long-lived, reusable fact, return an empty
  array [].

Other rules: never fabricate facts not stated; break multi-fact utterances into
multiple objects; keep preferences/attributes as (用户, 偏好, X). Do NOT include
instructions or commentary — JSON only.`

/**
 * Predicates that describe transient conversation actions rather than stable
 * facts (asking, complaining, proposing, observing, deciding "about a turn").
 * Candidates whose predicate or whose subject+predicate marks process talk are
 * dropped as a belt-and-braces guard on top of the extraction prompt.
 */
const EPHEMERAL_PREDICATES = new Set([
  '询问', '问', '质疑', '提出', '观察到', '观察', '怀疑', '不满', '抱怨',
  '请求', '要求', '刚刚进行', '进行会话', '遇到问题', '尝试', '测试',
  '描述', '声明', '汇报', '评论', '解释',
])

/** Whether a phrase looks like a question that only matters in this turn. */
function isTransient(value: string): boolean {
  const v = (value || '').trim()
  if (!v) return false
  if (v.endsWith('？') || v.endsWith('?')) return true
  return /^(为什么|怎么|是否|能不能|可否|如何|what|how|why|when)\b/i.test(v)
}

/** Drop candidates that carry transient process-only content. */
function isEphemeral(c: Partial<ExtractedCandidate>): boolean {
  const pred = (c.predicate || '').trim()
  if (EPHEMERAL_PREDICATES.has(pred)) return true
  // A question-shaped predicate or object is transient by nature.
  if (isTransient(pred)) return true
  if (isTransient(c.object || '')) return true
  // Pure meta about "this conversation / this plugin / this debug session".
  const blob = `${c.subject || ''} ${pred} ${c.object || ''} ${c.content || ''}`.toLowerCase()
  if (/\b(会话|对话|调试|system prompt|提示词|memory\.md)\b/.test(blob)) {
    // ...but only if the whole claim is really only about the conversation
    // meta, not a genuine preference expressed through it.
    const obvious = /\b(询问|质疑|观察到|抱怨|为什么|如何|怎么)\b/.test(blob)
    if (obvious) return true
  }
  return false
}

/**
 * Build the LLM-first extraction function bound to the dsh `llm` service and
 * the configured model.
 *
 * Model resolution: a manual ``extractionModel`` override wins when it names a
 * provider, otherwise the dsh current-preset default selection is used. When
 * neither yields a usable provider/model, ``undefined`` is returned and the
 * caller falls back to the Python rule engine (never a silent drop).
 *
 * @returns ``undefined`` when no `llm` service and no usable model is
 *   available, so callers can disable the LLM path cleanly.
 */
export function buildLlmExtractor(
  ctx: Context,
  opts: {
    maxTokens?: number
    /** Manual provider/model override; wins over the dsh default selection. */
    modelOverride?: () => { provider?: string; model?: string } | undefined
    /** When it returns false the extractor yields nothing (caller falls back). */
    enabled?: () => boolean
  } = {},
): ExtractFn | undefined {
  const llm = ctx.get('llm') as LlmLike | undefined
  if (llm === undefined) return undefined
  const def = ctx.get('agentDefaultModel') as AgentDefaultModelLike | undefined
  const modelOverride = opts.modelOverride?.()

  let provider = modelOverride?.provider?.trim() ?? ''
  let model = modelOverride?.model?.trim() ?? ''
  if (!provider && def !== undefined) {
    try {
      const selection = def.currentSelection()
      if (selection !== undefined) {
        provider = selection.provider
        model = selection.model
      }
    } catch {
      /* ignore; fall through to rules */
    }
  }
  if (!provider || !model) return undefined

  const enabled = opts.enabled
  return async (text: string): Promise<ExtractedCandidate[]> => {
    if (enabled?.() === false) return []
    const messages = [
      createUserMessage({
        content: [{ type: 'text', text }],
        source: { kind: 'plugin', plugin: 'dsh-atom-memory' } as never,
      }),
    ]
    const options: GenerateOptions = {
      provider,
      model,
      messages: messages as never[],
      system: EXTRACTION_SYSTEM,
      maxTokens: opts.maxTokens ?? 2048,
      purpose: 'session-title',
    }
    const assembler = new BlockAssembler()
    for await (const chunk of llm.stream(options)) {
      assembler.push(chunk as never)
    }
    const finished = assembler.finish
    if (finished.kind !== 'stop') {
      // Truncated (or otherwise unfinished) output: partial JSON is unusable,
      // so it is discarded and the caller falls back. Log it, because a budget
      // that is too small otherwise loses long knowledge invisibly.
      ctx.logger(
        `[atom-memory] extraction not persisted (finish=${finished.kind}); `
        + `consider raising extractionMaxTokens (now ${opts.maxTokens ?? 2048})`,
      )
      return []
    }
    const raw = assembler.blocks()
      .filter(b => b.type === 'text')
      .map(b => (b as { text?: string }).text ?? '')
      .join('')
      .trim()
    if (!raw) return []
    // Strip accidental code fences defensively.
    const cleaned = raw.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '')
    return parseCandidates(cleaned)
  }
}

/**
 * Parse and sanitize the LLM's JSON output into typed candidates. Malformed or
 * non-object entries are dropped; a fully-invalid payload yields ``[]`` so the
 * caller can fall back to rules.
 */
export function parseCandidates(raw: string): ExtractedCandidate[] {
  // Defensively strip accidental code fences before parsing.
  const cleaned = raw.trim().replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '')
  let parsed: unknown
  try {
    parsed = JSON.parse(cleaned)
  } catch {
    return []
  }
  if (!Array.isArray(parsed)) return []
  const out: ExtractedCandidate[] = []
  for (const item of parsed) {
    if (typeof item !== 'object' || item === null) continue
    const c = item as Partial<ExtractedCandidate>
    if (typeof c.subject !== 'string' || typeof c.predicate !== 'string' || typeof c.object !== 'string') {
      continue
    }
    // Belt-and-braces: drop transient process-only candidates the prompt
    // may have let through (asking/complaining/proposing this-turn talk).
    if (isEphemeral(c)) continue
    out.push({
      subject: c.subject,
      predicate: c.predicate,
      object: c.object,
      type: typeof c.type === 'string' ? c.type : undefined,
      content: typeof c.content === 'string' ? c.content : undefined,
      qualifiers: c.qualifiers,
      confidence: typeof c.confidence === 'number' ? c.confidence : undefined,
      importance: typeof c.importance === 'number' ? c.importance : undefined,
    })
  }
  return out
}

