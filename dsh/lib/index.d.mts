import z from "@deepseek-ai/schemastery";
import { Context } from "@deepseek-ai/cordis";
//#region src/config.d.ts
interface Config {
  /** Python-side SQLite database path (expanded by the library). */
  dbPath?: string;
  /** Override the interpreter used to spawn `python -m atom_memory.rpc`. */
  pythonBin?: string;
  /** Auto-start the bridge on plugin load (deployment-time switch). */
  autostart?: boolean;
  /** Master memory switch: when false the plugin is inert (no capture/context/tools). */
  enabled?: boolean;
  /** Manual LLM extraction model override; omit or leave provider empty to follow dsh default. */
  extractionModel?: {
    provider?: string;
    model?: string;
    /** Custom OpenAI-compatible endpoint base URL. When set, the extractor calls it directly. */
    baseURL?: string;
    /** Wire protocol the endpoint speaks (only `openai` supported). */
    protocol?: string;
    /** API key for a custom endpoint (plaintext). */
    apiKey?: string;
  };
  /** Whether the session/durable capture hooks (turn/end, user/message) run. */
  captureEnabled?: boolean;
  /** Whether the LLM-first extractor is wired to the dsh default model. */
  llmExtractionEnabled?: boolean;
  /**
   * Output-token cap for one LLM extraction call.
   *
   * The whole JSON payload (including any knowledge `content` body) must fit in
   * this budget: exceeding it truncates the response, and a truncated
   * extraction is discarded rather than persisted. Too small a value therefore
   * silently loses long-form knowledge.
   */
  extractionMaxTokens?: number;
  /** Whether the periodic nudge capture (write path) is enabled. */
  nudgeEnabled?: boolean;
  /** Minutes between periodic nudge sweeps. */
  nudgeIntervalMinutes?: number;
  /** Whether the pre-compression rescue hook is enabled. */
  preCompressionCapture?: boolean;
  /** Max facts surfaced to the model per recall tool call. */
  maxRecalledFacts?: number;
  /** Estimated token cap for returned summary. */
  summaryTokens?: number;
  /**
   * Token cap for the summary snapshot frozen into the system prompt.
   *
   * Deliberately separate from (and smaller than) `summaryTokens`: the
   * injected text is paid for on every request of a session and is rendered at
   * the compact depth, while the tool/settings view returns the full detail
   * list.
   *
   * This is only the *seed* for the live value: the settings panel owns it at
   * runtime (`atom-memory` → `injectedSummaryTokens`), and a change there
   * applies to every session that has not frozen its snapshot yet.
   */
  injectedSummaryTokens?: number;
  /** Inject a session-start-frozen summary snapshot into the system prompt. */
  contextInjectionEnabled?: boolean;
  /** Per-RPC timeout in ms. */
  rpcTimeoutMs?: number;
}
declare const Config: z<Config>;
//#endregion
//#region src/index.d.ts
declare const name = "dsh-atom-memory";
/**
 * Required services. `tools` and `systemPrompt` are the only hard
 * dependencies — matching the reference dsh-memory plugin. `llm`,
 * `agentDefaultModel` and `settings` are read via `ctx.get`, never injected
 * (they are optional, model-versioned, or deployment-determined services).
 */
declare const inject: readonly ["tools", "systemPrompt"];
declare function apply(ctx: Context, config: Config): void;
//#endregion
export { Config, apply, inject, name };