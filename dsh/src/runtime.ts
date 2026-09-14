/**
 * Runtime-configuration holder for the dsh-atom-memory plugin.
 *
 * The plugin's initial behaviour is taken from the composition-entry config
 * (schemastery), but the settings panel can change a handful of "live" fields
 * at runtime through the `atom-memory` settings namespace. Rather than tear
 * down and rebuild the whole plugin (which would drop registration state), the
 * behaviours consult this holder at each call site and react to
 * `onChange` notifications.
 *
 * The holder carries only the live, user-toggleable fields; every other config
 * field is read once from the composition entry at apply time. This keeps the
 * mutable surface small and auditable.
 */

import { clampInjectedMdTokens } from './injection-budget.ts'

export interface ExtractionModelOverride {
  /** Manual provider id (e.g. `deepseek`) or a free-form label for a custom endpoint. */
  provider?: string
  /** Manual model name. Leave empty to follow the dsh default selection. */
  model?: string
  /** Custom OpenAI-compatible endpoint base URL (e.g. `https://api.example.com/v1`). */
  baseURL?: string
  /** Wire protocol the endpoint speaks. Only `openai` is currently supported. */
  protocol?: string
  /** API key for a custom endpoint (plaintext, per the settings-panel design). */
  apiKey?: string
}

/** The live fields the settings panel can toggle at runtime. */
export interface LiveRuntime {
  /** Master switch: when false the plugin is inert (no writes, no context, no tools). */
  enabled: boolean
  /** Whether per-message / rescue / nudge capture runs. */
  captureEnabled: boolean
  /** Whether the LLM-first extractor is used (rule fallback stays). */
  llmExtractionEnabled: boolean
  /** Whether the per-session frozen snapshot + awareness are injected. */
  contextInjectionEnabled: boolean
  /**
   * Estimated-token budget for the snapshot injected into the system prompt.
   *
   * Live rather than apply-time because it is the knob a user actually tunes:
   * the snapshot is paid for on every request, so its size is the first thing
   * to want smaller. A change applies to every session that has not yet frozen
   * its snapshot — sessions already frozen keep their byte-identical text, so
   * the prompt prefix (and the provider's KV cache) stays valid.
   */
  injectedMemoryMdTokens: number
  /** Manual LLM extraction model override; empty provider+model = follow dsh default. */
  extractionModel?: ExtractionModelOverride
}

/** Values used to seed the runtime before the settings document exists. */
export interface LiveRuntimeSeed extends Partial<Omit<LiveRuntime, 'extractionModel'>> {
  extractionModel?: ExtractionModelOverride
}

/** Resolve a seed into a complete runtime value (defaults applied, budget clamped). */
export function createRuntime(seed: LiveRuntimeSeed): LiveRuntime {
  return {
    enabled: seed.enabled ?? true,
    captureEnabled: seed.captureEnabled ?? true,
    llmExtractionEnabled: seed.llmExtractionEnabled ?? true,
    contextInjectionEnabled: seed.contextInjectionEnabled ?? true,
    injectedMemoryMdTokens: clampInjectedMdTokens(seed.injectedMemoryMdTokens),
    extractionModel: seed.extractionModel,
  }
}

/** Mutable holder with a subscribe API for the settings `onChange` wiring. */
export class Runtime {
  private value: LiveRuntime
  private readonly listeners = new Set<() => void>()

  constructor(seed: LiveRuntime) {
    this.value = { ...seed }
  }

  /** Snapshot of the current live values. */
  get(): LiveRuntime {
    return { ...this.value }
  }

  /** Whether the plugin master switch is on. */
  isEnabled(): boolean {
    return this.value.enabled
  }

  /** Replace the whole live runtime (from a settings write). */
  set(next: LiveRuntime): void {
    const changed =
      this.value.enabled !== next.enabled ||
      this.value.captureEnabled !== next.captureEnabled ||
      this.value.llmExtractionEnabled !== next.llmExtractionEnabled ||
      this.value.contextInjectionEnabled !== next.contextInjectionEnabled
    // The injection budget is deliberately *not* part of `changed`: nothing
    // subscribes to re-wire it. It is read at each snapshot freeze (see
    // context.ts), which is exactly the moment a larger/smaller budget should
    // take effect, so no notification is needed.
    this.value = { ...next, injectedMemoryMdTokens: clampInjectedMdTokens(next.injectedMemoryMdTokens) }
    if (changed) {
      for (const listener of this.listeners) listener()
    }
  }

  /** Subscribe to runtime changes (returns the disposer). */
  subscribe(listener: () => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }
}

/** Namespace id used for the plugin's settings section on the Host. */
export const SETTINGS_NAMESPACE = 'atom-memory'
