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
import z from '@deepseek-ai/schemastery'
import { Config, type Config as ConfigShape } from './config.ts'
import { PythonBridge, defaultSpawn } from './bridge.ts'
import { registerMemoryTools } from './tools.ts'
import { registerMemoryContext } from './context.ts'
import { registerCapture } from './capture.ts'
import { buildLlmExtractor, type ExtractFn } from './llm-extractor.ts'
import {
  createRuntime, SETTINGS_NAMESPACE, type LiveRuntime, Runtime,
} from './runtime.ts'
import { AtomMemoryController } from './controller.ts'

export const name = 'dsh-atom-memory'
/**
 * Required services. `tools` and `systemPrompt` are the only hard
 * dependencies — matching the reference dsh-memory plugin. `llm`,
 * `agentDefaultModel` and `settings` are read via `ctx.get`, never injected
 * (they are optional, model-versioned, or deployment-determined services).
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

/**
 * Seed the live runtime from the composition config, applying defaults.
 * @param config - the validated composition entry.
 */
function seedRuntime(config: ConfigShape): LiveRuntime {
  return createRuntime({
    enabled: config.enabled !== false,
    captureEnabled: config.captureEnabled !== false,
    llmExtractionEnabled: config.llmExtractionEnabled !== false,
    contextInjectionEnabled: config.contextInjectionEnabled !== false,
    extractionModel: config.extractionModel,
  })
}

export function apply(ctx: Context, config: ConfigShape): void {
  const runtime = new Runtime(seedRuntime(config))

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

  // LLM-first extraction (optional): prefers a manual extractionModel override,
  // else follows the dsh default model. `enabled` gates the extractor so the
  // master switch silences the LLM path without re-registering anything.
  const extract: ExtractFn | undefined =
    runtime.get().llmExtractionEnabled === false
      ? undefined
      : buildLlmExtractor(ctx, {
          maxTokens: config.extractionMaxTokens ?? 2048,
          modelOverride: () => runtime.get().extractionModel,
          enabled: () => runtime.isEnabled(),
        })

  // The panel's data operations (features 3-5) are served to the browser over
  // the Remote gateway; registration is reversible with the controller. The
  // gateway protocol is optional — if this deployment lacks it, features 3-5
  // are simply unavailable in the browser and the plugin degrades gracefully.
  try {
    new AtomMemoryController(ctx, bridge, runtime)
  } catch (err) {
    ctx.logger(`[atom-memory] remote controller unavailable (${(err as Error)?.message ?? err})`)
  }

  // Single ingestion point: LLM-first candidates -> persist_candidates, else
  // -> rule-based add (everything stays isolated in the Python process).
  const capture = async (text: string, sessionId: string): Promise<void> => {
    if (!runtime.isEnabled()) return
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
    isEnabled: () => runtime.isEnabled(),
  })
  for (const d of disposers) ctx.effect(() => d)

  // Durable capture hooks (per-message, pre-compression, periodic nudge).
  registerCapture(
    { ctx, capture, maxRecent: 20 },
    {
      captureEnabled: runtime.get().captureEnabled,
      preCompressionCapture: config.preCompressionCapture !== false,
      nudgeEnabled: config.nudgeEnabled !== false,
      nudgeIntervalMs: (config.nudgeIntervalMinutes ?? 30) * 60_000,
    },
  ).forEach((d) => ctx.effect(() => d))

  // System-prompt awareness + the session-start-frozen memory snapshot.
  // `isEnabled` gates snapshot injection; the awareness section is registered
  // always (it is a static capability description) but injection stops when
  // the master switch is off.
  registerMemoryContext({
    ctx,
    bridge,
    userScope: FALLBACK_SCOPE,
    maxTokens: config.memoryMdTokens ?? 1500,
    snapshotEnabled: runtime.get().contextInjectionEnabled,
    isEnabled: () => runtime.isEnabled(),
  })

  // Settings namespace: the composition entry seeds the runtime; a settings
  // write replaces it live. This powers the memory master switch (feature 1)
  // and the LLM extraction model override (feature 2) without a restart.
  const settings = ctx.get('settings') as {
    installSection?(
      owner: Context,
      ns: string,
      schema: unknown,
      entry: LiveRuntime,
      hooks: {
        setSource(current: () => LiveRuntime): void
        onChange(): void
        validate?(value: LiveRuntime): void
      },
    ): void
  } | undefined
  if (settings?.installSection !== undefined) {
    let source: () => LiveRuntime = () => seedRuntime(config)
    settings.installSection(ctx, SETTINGS_NAMESPACE, LiveSettingsSchema, source(), {
      setSource: (current) => { source = current },
      onChange: () => { runtime.set(source()) },
    })
    ctx.logger(`[dsh-atom-memory] settings section "${SETTINGS_NAMESPACE}" registered`)
  }

  ctx.logger('[dsh-atom-memory] loaded')
}

/**
 * Schemastery schema for the live settings namespace. This mirrors only the
 * runtime-toggleable fields so a settings write maps 1:1 onto the Runtime.
 */
const LiveSettingsSchema: z<LiveRuntime> = z.object({
  enabled: z.boolean().default(true),
  captureEnabled: z.boolean().default(true),
  llmExtractionEnabled: z.boolean().default(true),
  contextInjectionEnabled: z.boolean().default(true),
  extractionModel: z.object({
    provider: z.string().default(''),
    model: z.string().default(''),
  }).default({ provider: '', model: '' }),
})


