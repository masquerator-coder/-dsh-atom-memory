/**
 * The `summary` injection budget: one authority shared by both halves.
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
export const DEFAULT_INJECTED_SUMMARY_TOKENS = 800

/**
 * Lower bound. Below this the renderer cannot fit even its "everything was
 * omitted" footer, so the snapshot would degrade to a single notice line —
 * never useful as a configured value.
 */
export const MIN_INJECTED_SUMMARY_TOKENS = 100

/**
 * Upper bound. Far above any sane working set, but it exists so a typo (or a
 * pasted number) cannot silently inflate every request of every session.
 */
export const MAX_INJECTED_SUMMARY_TOKENS = 20_000

/**
 * The gear ladder the settings panel's slider snaps to, smallest first.
 *
 * The panel offers fixed gears rather than a free number: the budget is paid on
 * every request of a session, so a slipped digit (8000 instead of 800) would
 * silently multiply the recurring cost of every new session, and a free field
 * has no way to show the user which side of "cheap / expensive" they landed on.
 * The ladder spans "one screen of headline memory" (300) to "practically the
 * whole store" (12000); the budget is a *cap*, not a target, so a large gear
 * costs nothing while the store is smaller than it.
 */
export const INJECTED_SUMMARY_TOKEN_PRESETS = [300, 800, 1500, 3000, 6000, 12_000] as const

/**
 * Index of the ladder gear nearest to `value`.
 *
 * The panel's slider is positioned by this index, so a settings document that
 * holds an off-ladder number (a value typed into the old free-text field, or
 * set from the plugin composition) still parks the handle next to the gear it
 * is closest to. Ties go to the smaller gear — the conservative side, since the
 * budget is a recurring cost. Unusable values clamp first, so they resolve to
 * the default's gear rather than to `NaN`.
 *
 * @param value - The configured budget, from settings or the composition entry.
 * @returns A valid index into {@link INJECTED_SUMMARY_TOKEN_PRESETS}.
 */
export function nearestInjectedSummaryPresetIndex(value: unknown): number {
  const tokens = clampInjectedSummaryTokens(value)
  let best = 0
  let bestDelta = Number.POSITIVE_INFINITY
  for (let i = 0; i < INJECTED_SUMMARY_TOKEN_PRESETS.length; i += 1) {
    const delta = Math.abs(INJECTED_SUMMARY_TOKEN_PRESETS[i]! - tokens)
    if (delta < bestDelta) {
      bestDelta = delta
      best = i
    }
  }
  return best
}

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
export function clampInjectedSummaryTokens(value: unknown): number {
  if (value === undefined || value === null) return DEFAULT_INJECTED_SUMMARY_TOKENS
  if (typeof value === 'string' && value.trim() === '') return DEFAULT_INJECTED_SUMMARY_TOKENS
  const tokens = Math.trunc(Number(value))
  if (!Number.isFinite(tokens)) return DEFAULT_INJECTED_SUMMARY_TOKENS
  if (tokens < MIN_INJECTED_SUMMARY_TOKENS) return MIN_INJECTED_SUMMARY_TOKENS
  if (tokens > MAX_INJECTED_SUMMARY_TOKENS) return MAX_INJECTED_SUMMARY_TOKENS
  return tokens
}
