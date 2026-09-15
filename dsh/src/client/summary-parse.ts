/**
 * Lightweight parser for the memory-summary markdown returned by the Host
 * `AtomMemoryController.summary` (backed by Python `AtomMem.summary`).
 *
 * The Python digest renders a single markdown document shaped like:
 *
 *     # 摘要 (Summary) — <user>
 *
 *     ## <scope> (v<version>)
 *
 *     <compressed body — attributes/preferences/workflows/events/knowledge
 *      joined with Chinese semicolons "；" on one line>
 *
 *     > ⚠ 另有 N 条长文知识（SOP/few-shot）未展开正文，需要时用 memory_recall 检索，或直接查看 fact_id: …
 *     > 覆盖 N 条活跃事实 · fact_id: …
 *
 * This module converts that text into a structured list the settings panel can
 * render as readable list items (instead of dumping the raw markdown in a
 * `<pre>`). It is a pure function over the Host's text contract, so it holds no
 * DSH-dev dependency and is verifiable offline with plain-node tests.
 *
 * @module dsh-atom-memory/client/summary-parse
 */

/** One `## <scope> (v<version>)` group. */
export interface SummarySection {
  /** The scope / theme heading with any `(vN)` suffix stripped (e.g. `global`). */
  theme: string
  /** The version suffix when present (e.g. `v3`), else `undefined`. */
  version?: string
  /** The compressed body split into list items (one per `；`-joined clause). */
  items: string[]
  /** A trailing `> ` caveat line (e.g. the long-form-knowledge note). */
  note?: string
  /** The trailing `> ` coverage line (`覆盖 N 条活跃事实 · fact_id: …`). */
  coverage?: string
}

/** The parsed shape the summary modal renders. */
export interface ParsedSummary {
  /** `true` when the body carried no `##` section headings (empty summary). */
  empty: boolean
  /** The parsed `##` groups, in document order. */
  sections: SummarySection[]
  /** The raw body when there were no sections (used for the empty-notice text). */
  rawText?: string
}

/** The Chinese full-width semicolon the Python `_aggregate` joins items with. */
const JOIN_SEPARATOR = '；'

/** Strip a leading `# ` or `## ` heading marker. */
function stripHeading(line: string): string {
  return line.replace(/^#+\s+/, '')
}

/** Parse a `## theme (vN)` heading into `{ theme, version }`. */
function parseSectionHeading(heading: string): { theme: string; version?: string } {
  const m = heading.match(/^(.*?)\s*\(v(\d+)\)\s*$/)
  if (m) return { theme: (m[1] ?? '').trim(), version: `v${m[2]}` }
  return { theme: heading.trim() }
}

/** Split a compressed body line into trimmed list items on the `；` separator. */
function splitItems(body: string): string[] {
  return body
    .split(JOIN_SEPARATOR)
    .map((part) => part.trim())
    .filter((part) => part.length > 0)
}

/**
 * Parse the Host summary markdown into a structured list.
 *
 * @param text The markdown string from `AtomMemoryController.summary` (non-empty).
 * @returns A structured {@link ParsedSummary}.
 */
export function parseSummary(text: string): ParsedSummary {
  const sections: SummarySection[] = []
  let current: SummarySection | undefined
  let rawText: string | undefined

  const lines = text.split(/\r?\n/)
  for (const line of lines) {
    const trimmed = line.trimEnd()
    if (trimmed.length === 0) continue

    if (trimmed.startsWith('## ')) {
      const { theme, version } = parseSectionHeading(stripHeading(trimmed))
      current = { theme, version, items: [] }
      sections.push(current)
      continue
    }

    // Skip the top-level `# 摘要 (Summary) …` document title.
    if (trimmed.startsWith('# ')) continue

    // Reference/caveat lines attach to the current section.
    if (trimmed.startsWith('> ')) {
      const body = trimmed.slice(2).trim()
      if (current !== undefined) {
        if (body.includes('覆盖')) current.coverage = body
        else current.note = current.note ? `${current.note}\n${body}` : body
      }
      continue
    }

    // Ordinary body text belongs to the current section (split into items) or,
    // when there is no section yet (the empty-summary notice), is raw.
    if (current !== undefined) {
      current.items.push(...splitItems(trimmed))
    } else {
      rawText = rawText ? `${rawText}\n${trimmed}` : trimmed
    }
  }

  return { empty: sections.length === 0, sections, rawText }
}
