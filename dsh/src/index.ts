/**
 * dsh-atom-memory — dsh-side integration for the Python memory library.
 *
 * A Cordis plugin that:
 *  - spawns and manages the `atom_memory.rpc` Python child process,
 *  - exposes `memory_*` tools (add/recall/summary/forget/memory_md/user_md/stats),
 *  - wires an LLM-first extractor that uses the dsh default model and ships
 *    typed candidates to Python for persistence (rules remain the fallback),
 *  - registers durable capture hooks (per-message, pre-compression rescue,
 *    periodic nudge) so conversation turns into memory automatically,
 *  - injects a system-prompt awareness section plus a per-session memory
 *    snapshot frozen at the session's first prompt assembly (so the prompt
 *    prefix never changes mid-session and KV cache stays valid).
 *
 * It never modifies dsh source and never imports the Python library — all
 * memory lives in the isolated child process, reached over the NDJSON bridge.
 *
 * @module dsh-atom-memory/index
 */
import type { Context } from '@deepseek-ai/cordis'
import { Config, type Config as ConfigShape } from './config.ts'
import { PythonBridge, defaultSpawn } from './bridge.ts'
import { registerMemoryTools } from './tools.ts'
import { registerMemoryContext } from './context.ts'
import { registerCapture } from './capture.ts'
import { buildLlmExtractor, type ExtractFn } from './llm-extractor.ts'

export const name = 'dsh-atom-memory'
/**
 * Required services. `tools` and `systemPrompt` are the only hard
 * dependencies — matching the reference dsh-memory plugin. `llm` and
 * `agentDefaultModel` are read via `ctx.get`, never injected (they are
 * optional, model-versioned services).
 */
export const inject = ['tools', 'systemPrompt'] as const

export { Config }

/** Fallback user/session scope for a single-user local harness. */
const FALLBACK_SCOPE = 'global'

/** Start params sent to the Python bridge (worker/embedding config). */
function buildStartParams(config: ConfigShape): Record<string, unknown> {
  return {
    db_path: config.dbPath ?? '~/.dsh/atom-memory/memory.db',
    worker_poll_interval_sec: 0.5,
    summary_rebuild_debounce_sec: 5.0,
    max_retries: 3,
  }
}

export function apply(ctx: Context, config: ConfigShape): void {
  const bridge = new PythonBridge({
    spawnProcess: () => defaultSpawn(config.pythonBin),
    timeoutMs: config.rpcTimeoutMs,
    onEvent: (evt) => {
      ctx.logger(`[atom-memory] ${evt.evt as string} ${evt.candidate_id as string ?? ''}`.trim())
    },
    onLog: (msg) => ctx.logger(`[atom-memory] ${msg}`),
  })

  // All registration is reversible: the child process is killed on unload.
  ctx.effect(() => () => { void bridge.dispose() })

  const started = {
    value: false,
    error: undefined as Error | undefined,
    attempt: 0,
  }
  const tryStart = (): void => {
    if (started.value) return
    if (started.attempt > 3) {
      ctx.logger('[atom-memory] python bridge failed to start; memory offline')
      return
    }
    started.attempt += 1
    void bridge.start(buildStartParams(config), undefined)
      .then(() => {
        started.value = true
        started.error = undefined
        ctx.logger(`[atom-memory] bridge ready (${(config.dbPath ?? '').trim() || 'db'})`)
      })
      .catch((err: Error) => {
        started.error = err
        setTimeout(tryStart, 1000)
      })
  }
  if (config.autostart !== false) tryStart()

  // LLM-first extraction (optional): reads the dsh default model.
  const extract: ExtractFn | undefined =
    config.llmExtractionEnabled === false ? undefined : buildLlmExtractor(ctx, { maxTokens: 600 })

  // Single ingestion point: LLM-first candidates -> persist_candidates, else
  // -> rule-based add (everything stays isolated in the Python process).
  const capture = async (text: string, sessionId: string): Promise<void> => {
    if (started.value && extract !== undefined) {
      try {
        const candidates = await extract(text)
        if (candidates.length > 0) {
          await bridge.call('persist_candidates', {
            user_id: FALLBACK_SCOPE,
            session_id: sessionId,
            turn_id: 0,
            candidates,
          })
          return
        }
      } catch {
        /* fall through to rule extraction on LLM failure */
      }
    }
    if (started.value) {
      await bridge.call('add', {
        user_id: FALLBACK_SCOPE,
        session_id: sessionId,
        text,
        turn_id: 0,
      })
    }
  }

  // Explicit memory tools. `extract` is shared with the capture path so the
  // model-driven `memory_add` uses the same LLM-first extraction (with the
  // rule path as fallback) instead of the rules-only bridge `add` call.
  const disposers = registerMemoryTools({
    ctx,
    bridge,
    fallbackScope: FALLBACK_SCOPE,
    maxRecalledFacts: config.maxRecalledFacts ?? 10,
    memoryMdTokens: config.memoryMdTokens ?? 1500,
    extract,
  })
  for (const d of disposers) ctx.effect(() => d)

  // Durable capture hooks (per-message, pre-compression, periodic nudge).
  registerCapture(
    { ctx, capture, maxRecent: 20 },
    {
      captureEnabled: config.captureEnabled !== false,
      preCompressionCapture: config.preCompressionCapture !== false,
      nudgeEnabled: config.nudgeEnabled !== false,
      nudgeIntervalMs: (config.nudgeIntervalMinutes ?? 30) * 60_000,
    },
  ).forEach((d) => ctx.effect(() => d))

  // System-prompt awareness + the session-start-frozen memory snapshot.
  registerMemoryContext({
    ctx,
    bridge,
    userScope: FALLBACK_SCOPE,
    maxTokens: config.memoryMdTokens ?? 1500,
    snapshotEnabled: config.contextInjectionEnabled !== false,
  })

  ctx.logger('[dsh-atom-memory] loaded')
}


