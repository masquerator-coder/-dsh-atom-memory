/**
 * Unit tests for the summary-markdown parser (`src/client/summary-parse.ts`).
 *
 * Pure-function tests (node env). The parser turns the Host `summary` markdown
 * (Python `AtomMem.summary`) into a structured list the settings modal renders,
 * so the cases here pin the exact wire text contract: `##` section headings
 * with optional `(vN)`, `；`-joined body clauses, and `>` caveat/coverage lines.
 */
import { describe, expect, it } from 'vitest'
import { parseSummary } from '../src/client/summary-parse.ts'

describe('parseSummary', () => {
  it('parses one section and splits the semicolon-joined body into items', () => {
    const parsed = parseSummary(
      '# 摘要 (Summary) — global\n\n' +
      '## global (v3)\n\n' +
      '属性: 工程师；偏好 Python (喜欢)；工作流程-部署: 1)构建 2)发布；[2026-09] 加入团队\n\n' +
      '> 覆盖 4 条活跃事实 · fact_id: a, b, c, d',
    )

    expect(parsed.empty).toBe(false)
    expect(parsed.sections).toHaveLength(1)
    const sec = parsed.sections[0]!
    expect(sec.theme).toBe('global')
    expect(sec.version).toBe('v3')
    expect(sec.items).toEqual([
      '属性: 工程师',
      '偏好 Python (喜欢)',
      '工作流程-部署: 1)构建 2)发布',
      '[2026-09] 加入团队',
    ])
    expect(sec.coverage).toBe('覆盖 4 条活跃事实 · fact_id: a, b, c, d')
    expect(sec.note).toBeUndefined()
  })

  it('distinguishes the long-form-knowledge note from the coverage line', () => {
    const parsed = parseSummary(
      '# 摘要 (Summary) — global\n\n' +
      '## global\n\n' +
      '属性: 工程师\n\n' +
      '> ⚠ 另有 2 条长文知识（SOP/few-shot）未展开正文，需要时用 memory_recall 检索，或直接查看 fact_id: x, y\n' +
      '> 覆盖 3 条活跃事实 · fact_id: x, y, z',
    )

    const sec = parsed.sections[0]!
    expect(sec.note).toContain('另有 2 条长文知识')
    expect(sec.coverage).toContain('覆盖 3 条活跃事实')
  })

  it('parses multiple sections in document order', () => {
    const parsed = parseSummary(
      '# 摘要 (Summary) — global\n\n' +
      '## global (v1)\n\n属性: A\n\n' +
      '## plan (v2)\n\n偏好 X (喜欢)',
    )

    expect(parsed.sections.map((s) => s.theme)).toEqual(['global', 'plan'])
    expect(parsed.sections.map((s) => s.version)).toEqual(['v1', 'v2'])
    expect(parsed.sections[0]!.items).toEqual(['属性: A'])
    expect(parsed.sections[1]!.items).toEqual(['偏好 X (喜欢)'])
  })

  it('treats a heading-less body (empty summary notice) as raw and empty', () => {
    const parsed = parseSummary('# 摘要 (Summary) — global\n\n_暂无摘要。_ (No summary yet — no active facts.)\n')

    expect(parsed.empty).toBe(true)
    expect(parsed.sections).toHaveLength(0)
    expect(parsed.rawText).toContain('暂无摘要')
  })

  it('handles a section heading without a version suffix', () => {
    const parsed = parseSummary('# 摘要 (Summary) — global\n\n## 工作\n\n属性: 工程师')
    expect(parsed.sections[0]!.theme).toBe('工作')
    expect(parsed.sections[0]!.version).toBeUndefined()
  })

  it('ignores CRLF line endings and blank lines', () => {
    const parsed = parseSummary('# 摘要 (Summary) — global\r\n\r\n## global\r\n\r\n属性: A')
    expect(parsed.sections[0]!.items).toEqual(['属性: A'])
  })
})
