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
If nothing worth remembering, return an empty array [].

Rules: never fabricate facts not stated; break multi-fact utterances into
multiple objects; keep preferences/attributes as (用户, 偏好, X). Do NOT include
instructions or commentary — JSON only.`

/**
 * Build the LLM-first extraction function bound to the dsh `llm` service and
 * the current default model.
 *
 * @returns ``undefined`` when no `llm` service or no default model is
 *   available, so callers can disable the LLM path cleanly.
 */
export function buildLlmExtractor(
  ctx: Context,
  opts: { maxTokens?: number } = {},
): ExtractFn | undefined {
  const llm = ctx.get('llm') as LlmLike | undefined
  const def = ctx.get('agentDefaultModel') as AgentDefaultModelLike | undefined
  if (llm === undefined || def === undefined) return undefined
  let selection: { provider: string; model: string } | undefined
  try {
    selection = def.currentSelection()
  } catch {
    selection = undefined
  }
  if (selection === undefined || !selection.provider || !selection.model) {
    return undefined
  }

  return async (text: string): Promise<ExtractedCandidate[]> => {
    const messages = [
      createUserMessage({
        content: [{ type: 'text', text }],
        source: { kind: 'plugin', plugin: 'dsh-atom-memory' } as never,
      }),
    ]
    const options: GenerateOptions = {
      provider: selection!.provider,
      model: selection!.model,
      messages: messages as never[],
      system: EXTRACTION_SYSTEM,
      maxTokens: opts.maxTokens ?? 600,
      purpose: 'session-title',
    }
    const assembler = new BlockAssembler()
    for await (const chunk of llm.stream(options)) {
      assembler.push(chunk as never)
    }
    const finished = assembler.finish
    if (finished.kind !== 'stop') {
      return [] // let rules handle it rather than persisting partial output
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

