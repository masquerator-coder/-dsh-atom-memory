/**
 * Durable capture hooks.
 *
 * These hooks turn conversation into memory automatically and, crucially,
 * rescue facts that would otherwise be lost:
 *
 *  1. **Per-message capture** — `user/message` durable events whose text
 *     carries a fact-worthy signal are submitted for extraction
 *     immediately. Every direct user message is captured; whether it becomes
 *     a fact is decided downstream by the LLM extractor (or rule fallback),
 *     not by a brittle keyword gate.
 *  2. **Pre-compression rescue** — when the context window is about to be
 *     compacted (`llm/stream` with `purpose: 'compaction'`), the session's
 *     recent not-yet-stored user messages are re-scanned and saved *before*
 *     compression so key facts survive the fold. The hook always calls
 *     `next()` — compression is never blocked.
 *  3. **Periodic nudge** — a timer periodically re-scans recent direct user
 *     messages so facts the LLM was too busy to save are not lost.
 *
 * The per-session "recent messages" buffer is fed only from durable
 * `user/message` session events (which dsh logs and can replay), so the memory
 * written back is reproducible from the session log — it never reads live,
 * non-replayable coordination state.
 *
 * Every dispatch is best-effort: a capture failure never throws into the loop.
 *
 * @module dsh-atom-memory/capture
 */
import type { Context } from '@deepseek-ai/cordis'
import type { Session, SessionEvent } from '@deepseek-ai/dsh-session'
import type { GenerateOptions, StreamChunk } from '@deepseek-ai/dsh-llm'

interface MessageEntry {
  seq: number
  text: string
  /** True once this message has been durably captured (or surrendered to a rescue retry). */
  captured: boolean
  /** True when the immediate per-message capture failed and is therefore retriable by the nudge/compaction sweep. */
  failed: boolean
}

export interface CaptureDeps {
  ctx: Context
  /** Enqueue text for extraction (LLM-first or rule fallback; caller-owned). */
  capture: (text: string, sessionId: string) => Promise<void>
  /** Max recent messages remembered per session. */
  maxRecent?: number
}

export interface CaptureOptions {
  captureEnabled: boolean
  preCompressionCapture: boolean
  nudgeEnabled: boolean
  /** Nudge sweep period in ms. */
  nudgeIntervalMs: number
}

/** Pull the plain text out of a user message's content blocks. */
function userMessageText(event: SessionEvent): string {
  const data = event.data as { content?: Array<{ type?: string; text?: string }> }
  const blocks = data.content ?? []
  if (blocks.length === 0) return ''
  const first = blocks[0]
  return first?.type === 'text' ? (first.text ?? '') : ''
}

/** Whether a user message is a genuine human prompt (vs. plugin-sourced). */
function isDirectUserMessage(event: SessionEvent): boolean {
  const source = (event.data as { source?: { kind?: string } }).source
  return source?.kind === 'user'
}

/**
 * Register all capture hooks and return their disposers.
 */
export function registerCapture(deps: CaptureDeps, opts: CaptureOptions): (() => void)[] {
  const disposers: (() => void)[] = []
  const { ctx, capture } = deps
  const maxRecent = deps.maxRecent ?? 20
  const recent = new Map<string, MessageEntry[]>()

  const push = (sessionId: string, entry: MessageEntry): void => {
    const list = recent.get(sessionId) ?? []
    list.push(entry)
    while (list.length > maxRecent) list.shift()
    recent.set(sessionId, list)
  }

  /**
   * Re-scan recent messages, retrying those whose immediate capture failed.
   *
   * Only entries marked ``failed`` are retried: an entry whose immediate
   * capture is still in-flight (neither succeeded nor failed) is skipped so a
   * rescue cannot duplicate it, and an already-``captured`` one is skipped too.
   * Each entry is marked ``captured`` *before* the retry is awaited so two
   * concurrent sweeps (pre-compression + nudge) cannot double-send the same
   * text.
   */
  const sweep = async (sessionId: string): Promise<void> => {
    const list = recent.get(sessionId)
    if (!list) return
    for (const entry of list) {
      if (entry.captured || !entry.failed) continue
      entry.captured = true // claim; a concurrent sweep must not resend it
      await capture(entry.text, sessionId).catch(() => { /* best-effort */ })
    }
  }

  // -- per-message capture (durable) -----------------------------------------
  if (opts.captureEnabled) {
    disposers.push(ctx.on('session/event', (session: Session, event: SessionEvent) => {
      if (event.type !== 'user/message') return
      if (!isDirectUserMessage(event)) return
      const text = userMessageText(event)
      if (text.trim().length === 0) return
      const seq = (event as { seq?: unknown }).seq as number | undefined ?? 0
      // Capture every direct user message unconditionally. Whether it holds a
      // fact worth remembering is decided downstream by the LLM extractor (or
      // the rule fallback), not by a fixed keyword list. The entry is *not*
      // marked captured up front: a failed immediate capture must be retriable
      // by the nudge / pre-compression rescue, otherwise a bridge outage would
      // silently lose the message forever. Only a successful capture (or a
      // rescue retry) marks it captured; a failure marks it `failed` so
      // `sweep` retries it.
      const entry: MessageEntry = { seq, text, captured: false, failed: false }
      push(session.id, entry)
      void capture(text, session.id).then(
        () => { entry.captured = true },
        () => { entry.failed = true },
      )
    }))
  }

  // -- pre-compression rescue (llm/stream waterfall) -------------------------
  if (opts.preCompressionCapture) {
    disposers.push(ctx.on('llm/stream', async function* (options: GenerateOptions, next) {
      if (options.purpose === 'compaction' && options.sessionId) {
        // Rescue not-yet-stored messages before the compaction request leaves.
        // Never blocks the request itself.
        try {
          await sweep(String(options.sessionId))
        } catch {
          /* best-effort */
        }
      }
      yield* await next()
    }))
  }

  // -- periodic nudge (timer, best-effort) -----------------------------------
  if (opts.nudgeEnabled) {
    const timer = setInterval(() => {
      // Sweep every session we've seen so far; guards against the LLM being
      // too busy to trigger capture mid-turn. Off the loop's hot path.
      for (const sessionId of recent.keys()) {
        void sweep(sessionId).catch(() => { /* best-effort */ })
      }
    }, Math.max(opts.nudgeIntervalMs, 1000))
    disposers.push(() => clearInterval(timer))
  }

  return disposers
}

// Re-export the StreamChunk type so the waterfall page's return type stays
// coherent for callers that import from here.
export type { StreamChunk }
