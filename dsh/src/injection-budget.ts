/**
 * The `memory.md` injection budget: one authority shared by both halves.
 *
 * The Node half clamps whatever reaches the runtime before it is handed to the
 * Python renderer; the browser half needs the very same bounds to render the
 * preset ladder and to clamp a typed value before writing it. Keeping the
 * numbers here — instead of once per half — is what stops the two from drifting
 * apart, and the module is deliberately dependency-free so the browser bundle
 * can inline it.
 *
 * The budget is an estimated-token cap on the snapshot frozen into the session
 * system prompt. It is paid for on *every* request of a session, so it is the
 * one memory knob with a direct, recurring cost.
 *
 * @module dsh-atom-memory/injection-budget
 */

/** Budget used when nothing (neither settings nor composition) specifies one. */
export const DEFAULT_INJECTED_MD_TOKENS = 800

/**
 * Lower bound. Below this the renderer cannot fit even its "everything was
 * omitted" footer, so the snapshot would degrade to a single notice line —
 * never useful as a configured value.
 */
export const MIN_INJECTED_MD_TOKENS = 100

/**
 * Upper bound. Far above any sane working set, but it exists so a typo (or a
 * pasted number) cannot silently inflate every request of every session.
 */
export const MAX_INJECTED_MD_TOKENS = 20_000

/** The preset ladder offered by the settings panel, smallest first. */
export const INJECTED_MD_TOKEN_PRESETS = [300, 800, 1500] as const

/**
 * Coerce an arbitrary value into a usable budget.
 *
 * Applied on the Host before the value reaches the renderer, so a malformed
 * settings document (missing field, string, `NaN`, negative) degrades to a
 * working budget instead of breaking prompt assembly or the Python render.
 *
 * "Nothing was provided" (`undefined`, `null`, an empty/blank string) falls back
 * to the default rather than to the lower bound: a cleared field or an absent
 * settings key means *unset*, not "the smallest budget allowed". A supplied but
 * unusable number (`'abc'`, `NaN`, `Infinity`) is likewise treated as unset,
 * whereas a supplied out-of-range number snaps to the nearest bound, which is
 * what the panel shows the user.
 *
 * @param value - The candidate budget, from settings or the composition entry.
 * @returns An integer within `[MIN, MAX]`; the default when not provided.
 */
export function clampInjectedMdTokens(value: unknown): number {
  if (value === undefined || value === null) return DEFAULT_INJECTED_MD_TOKENS
  if (typeof value === 'string' && value.trim() === '') return DEFAULT_INJECTED_MD_TOKENS
  const tokens = Math.trunc(Number(value))
  if (!Number.isFinite(tokens)) return DEFAULT_INJECTED_MD_TOKENS
  if (tokens < MIN_INJECTED_MD_TOKENS) return MIN_INJECTED_MD_TOKENS
  if (tokens > MAX_INJECTED_MD_TOKENS) return MAX_INJECTED_MD_TOKENS
  return tokens
}
