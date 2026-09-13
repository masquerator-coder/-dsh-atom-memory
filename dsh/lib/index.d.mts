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
  /** Whether the session/durable capture hooks (turn/end, user/message) run. */
  captureEnabled?: boolean;
  /** Whether the LLM-first extractor is wired to the dsh default model. */
  llmExtractionEnabled?: boolean;
  /** Whether the periodic nudge capture (write path) is enabled. */
  nudgeEnabled?: boolean;
  /** Minutes between periodic nudge sweeps. */
  nudgeIntervalMinutes?: number;
  /** Whether the pre-compression rescue hook is enabled. */
  preCompressionCapture?: boolean;
  /** Max facts surfaced to the model per recall tool call. */
  maxRecalledFacts?: number;
  /** Estimated token cap for returned memory.md. */
  memoryMdTokens?: number;
  /** Inject a session-start-frozen memory.md snapshot into the system prompt. */
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
 * dependencies — matching the reference dsh-memory plugin. `llm` and
 * `agentDefaultModel` are read via `ctx.get`, never injected (they are
 * optional, model-versioned services).
 */
declare const inject: readonly ["tools", "systemPrompt"];
declare function apply(ctx: Context, config: Config): void;
//#endregion
export { Config, apply, inject, name };