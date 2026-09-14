"""Tests for ``memory.md`` rendering (``memory_md.py``).

Covers both render depths and, above all, the properties the old implementation
got wrong: memory types stay distinct instead of collapsing into one flat list;
ordering follows a real priority signal rather than plain recency (``importance``
is only a signal when the extractor supplied one; the neutral default defers to
the type's rank); and a tight token budget keeps the **most important and most
recent** memory rather than gutting whichever section sorts last.
"""

from __future__ import annotations

import re

from atom_memory.db import connect_for_tests
from atom_memory.models import NEUTRAL_SCORE
from atom_memory.memory_md import (
    _DETAIL_CONTENT_CHARS,
    _MAX_COMPACT_LINE_CHARS,
    _MAX_DETAIL_FIELD_CHARS,
    _MAX_FOLDED_VALUE_CHARS,
    _RECENCY_HALF_LIFE_SECONDS,
    generate_memory_md,
)
from atom_memory.retriever import estimate_tokens

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# Ages are expressed in half-lives so the tests read as "N half-lives staler".
HALF_LIFE = _RECENCY_HALF_LIFE_SECONDS


def _insert_fact(
    conn,
    fact_id: str,
    predicate: str,
    obj: str,
    memory_type: str = "semantic",
    importance: float = NEUTRAL_SCORE,
    confidence: float = NEUTRAL_SCORE,
    content: str | None = None,
    qualifiers: str | None = None,
    user_id: str = "u1",
    created_at: int = 1000,
    subject: str = "用户",
):
    """Insert one active fact directly, bypassing the worker pipeline."""
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, qualifiers, confidence, importance, type, content, status, "
        "observed_at, created_at, version) "
        "VALUES (?, ?, 's1', ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, 1)",
        (
            fact_id, user_id, subject, predicate, obj, qualifiers,
            confidence, importance, memory_type, content, created_at, created_at,
        ),
    )
    conn.commit()


def _md(conn, max_tokens: int = 1500, detail: bool = False, user_id: str = "u1"):
    return generate_memory_md(conn, user_id, max_tokens, detail)


# ---- compact depth -----------------------------------------------------------


def test_compact_omits_fact_id_and_scores():
    """The injected view carries no UUIDs and none of the uniform score noise."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "aaaa1111-2222-3333-4444-555566667777", "职业", "工程师")
        md = _md(conn)

        assert "aaaa1111" not in md
        assert not UUID_RE.search(md)
        assert "置信" not in md
        assert "重要" not in md
    finally:
        conn.close()


def test_compact_has_no_markdown_title():
    """No ``# 记忆 (Memory)`` heading: the injection site supplies its own."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "职业", "工程师")
        md = _md(conn)
        assert not md.splitlines()[0].startswith("# 记忆")
        assert "global" not in md
    finally:
        conn.close()


def test_compact_groups_every_present_type():
    """All memory kinds the user actually has get their own section."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "决定", "先回滚再排查", memory_type="decision_rule")
        _insert_fact(conn, "f2", "教训", "上线前必须测试", memory_type="lesson")
        _insert_fact(conn, "f3", "发布流程", "步骤", memory_type="procedural")
        _insert_fact(conn, "f4", "职业", "工程师", memory_type="semantic")
        _insert_fact(conn, "f5", "偏好", "黑咖啡", memory_type="semantic")
        md = _md(conn)
        lines = md.splitlines()

        for title in ("决策规则", "教训", "流程", "偏好", "属性"):
            assert title in lines, title
        # A section label is never rendered without content under it.
        for index, line in enumerate(lines[:-1]):
            if line in ("决策规则", "教训", "流程", "偏好", "属性"):
                assert lines[index + 1].startswith("- "), line
    finally:
        conn.close()


# ---- detail depth ------------------------------------------------------------


def test_detail_keeps_fact_id_and_title():
    """The settings/tool view still exposes ``fact_id`` so facts stay editable."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "职业", "工程师")
        md = _md(conn, detail=True)

        assert md.splitlines()[0].startswith("# 记忆 (Memory) — u1")
        assert "[f1]" in md
        assert "fact_id" in md
    finally:
        conn.close()


def test_detail_renders_knowledge_body():
    """A knowledge fact's body is addressable from the detail view."""
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "f1", "教训", "不能在没测试的情况下直接上线",
            memory_type="lesson", content="不能在没测试的情况下直接上线",
        )
        md = _md(conn, detail=True)
        assert "不能在没测试的情况下直接上线" in md
    finally:
        conn.close()


# ---- per-line length cap -----------------------------------------------------


def _bullet_lines(md: str) -> list[str]:
    """Return the rendered content lines (the ones the reader/model actually reads)."""
    return [line for line in md.splitlines() if line.startswith("- ")]


def test_compact_line_cap_bounds_every_rendered_line():
    """每条注入行都被限制在 80 字符内，无论它由什么拼成。

    Covers every compact line shape at once: a folded attribute, a folded
    preference list, a knowledge-body headline, and an episodic line with a
    ``[when]`` prefix.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "a", "属性", "值" * 400)
        _insert_fact(conn, "b", "偏好", "长偏好" * 200)
        _insert_fact(
            conn, "c", "教训", "标题", memory_type="lesson", content="正文" * 500,
        )
        _insert_fact(
            conn, "d", "事件", "发生了一件很长的" * 40,
            memory_type="episodic", qualifiers='{"when": "2026-02-01"}',
        )
        md = _md(conn)

        lines = _bullet_lines(md)
        assert len(lines) >= 4
        for line in lines:
            assert len(line) <= _MAX_COMPACT_LINE_CHARS, (len(line), line)
    finally:
        conn.close()


def test_compact_line_cap_does_not_touch_short_folds():
    """短值的折叠行不受影响：每个值都仍然可见。"""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "偏好", "黑咖啡", created_at=3000)
        _insert_fact(conn, "f2", "偏好", "少糖", created_at=2000)
        _insert_fact(conn, "f3", "偏好", "深色主题", created_at=1000)
        md = _md(conn)

        line = next(l for l in _bullet_lines(md) if l.startswith("- 黑咖啡"))
        assert "少糖" in line
        assert "深色主题" in line
        assert len(line) <= _MAX_COMPACT_LINE_CHARS
    finally:
        conn.close()


def test_compact_line_cap_clips_a_runaway_value_before_folding():
    """超长值先被单独截断，不会独占整行而让同谓词的其他值彻底消失。

    The point of clipping values *before* joining them: without it the line cap
    would consume the entire line with the first value and the reader would never
    learn that a second value exists.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "部署路径", "A" * 400, created_at=2000)
        _insert_fact(conn, "f2", "部署路径", "B" * 400, created_at=1000)
        md = _md(conn)

        line = next(l for l in _bullet_lines(md) if l.startswith("- 部署路径"))
        assert len(line) <= _MAX_COMPACT_LINE_CHARS
        values = line.split(": ", 1)[1].split("、")
        assert len(values) >= 2, line
        for value in values:
            assert len(value) <= _MAX_FOLDED_VALUE_CHARS, value
    finally:
        conn.close()


def test_detail_clips_each_field_and_keeps_the_fact_id_readable():
    """完整版逐字段限长，但 fact_id 与结构保持完整——它才是这一层存在的理由。"""
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "f1", "P" * 400, "O" * 400, subject="S" * 400,
        )
        md = _md(conn, detail=True)

        line = next(l for l in md.splitlines() if l.startswith("- ["))
        assert line.startswith("- [f1] ")
        assert "S" * (_MAX_DETAIL_FIELD_CHARS + 1) not in line
        assert "P" * (_MAX_DETAIL_FIELD_CHARS + 1) not in line
        assert "O" * (_MAX_DETAIL_FIELD_CHARS + 1) not in line
        # The clipped fields are still marked as clipped.
        assert line.count("…") == 3
    finally:
        conn.close()


def test_detail_clips_the_knowledge_body_subline():
    """完整版的知识正文折叠行同样限长。"""
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "f1", "教训", "短", memory_type="lesson", content="正文" * 300,
        )
        md = _md(conn, detail=True)

        sub = next(l for l in md.splitlines() if "知识内容" in l)
        snippet = sub.split("知识内容** ", 1)[1]
        assert len(snippet) <= _DETAIL_CONTENT_CHARS
    finally:
        conn.close()


def test_line_cap_keeps_one_long_memory_from_crowding_out_others():
    """限长的实际收益：一条超长记忆不再挤掉同一预算下的其他记忆。"""
    conn = connect_for_tests()
    try:
        # One runaway knowledge body plus four ordinary facts.
        _insert_fact(
            conn, "big", "教训", "巨大教训", memory_type="lesson",
            content="很长的正文" * 500, created_at=9000,
        )
        for index in range(4):
            _insert_fact(
                conn, f"n{index}", "属性", f"普通值{index}",
                memory_type="semantic", created_at=1000 + index,
            )
        md = _md(conn, max_tokens=200)

        lines = _bullet_lines(md)
        assert all(len(line) <= _MAX_COMPACT_LINE_CHARS for line in lines)
        # The four ordinary facts are all still represented.
        for index in range(4):
            assert f"普通值{index}" in md, index
        assert estimate_tokens(md) <= 200
    finally:
        conn.close()


# ---- priority ----------------------------------------------------------------


def test_type_default_breaks_the_neutral_tie():
    """Facts with no explicit signal rank by type, not by insertion order."""
    conn = connect_for_tests()
    try:
        # The semantic fact is *newer*: recency ordering would put it first.
        _insert_fact(conn, "f1", "职业", "工程师", created_at=2000)
        _insert_fact(
            conn, "f2", "决定", "先回滚再排查",
            memory_type="decision_rule", created_at=1000,
        )
        md = _md(conn)
        assert md.index("先回滚再排查") < md.index("工程师")
    finally:
        conn.close()


def test_stated_importance_outranks_the_type_default():
    """An explicit signal from the extractor wins over the type fallback."""
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "f1", "决定", "先回滚再排查",
            memory_type="decision_rule", importance=NEUTRAL_SCORE,
        )
        _insert_fact(
            conn, "f2", "部署路径", "C:\\dsh",
            memory_type="semantic", importance=0.95,
        )
        md = _md(conn)
        assert md.index("C:\\dsh") < md.index("先回滚再排查")
    finally:
        conn.close()


# ---- folding -----------------------------------------------------------------


def test_multi_valued_values_fold_onto_one_line():
    """Repeated preferences do not each burn a bullet."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "偏好", "黑咖啡", created_at=3000)
        _insert_fact(conn, "f2", "偏好", "少糖", created_at=2000)
        md = _md(conn)

        preference_lines = [
            line for line in md.splitlines()
            if line.startswith("- ") and "黑咖啡" in line
        ]
        assert len(preference_lines) == 1
        assert "少糖" in preference_lines[0]
    finally:
        conn.close()


def test_negated_preference_is_marked():
    """A disliked value is rendered separately from liked ones."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "偏好", "黑咖啡", created_at=2000)
        _insert_fact(
            conn, "f2", "偏好", "奶茶",
            qualifiers='{"negation": true}', created_at=1000,
        )
        md = _md(conn)
        assert "奶茶（不喜欢）" in md
        assert "黑咖啡（不喜欢）" not in md
    finally:
        conn.close()


# ---- budgeting ---------------------------------------------------------------


def test_budget_is_a_hard_cap_including_the_footer():
    """The rendered artifact never exceeds the caller's token budget."""
    conn = connect_for_tests()
    try:
        for index in range(40):
            _insert_fact(
                conn, f"f{index}", f"属性{index}",
                "值" * 20,
                memory_type="semantic",
                content="正文" * 40,
                created_at=1000 + index,
            )
        # 30 is below the cost of the one-line "omitted" notice (~18 tokens plus
        # its section text), so the documented floor is the lowest budget at
        # which the cap is meaningful for real configuration.
        for budget in (30, 60, 120, 200, 300, 500, 800):
            md = _md(conn, max_tokens=budget)
            assert estimate_tokens(md) <= budget, (budget, estimate_tokens(md))
    finally:
        conn.close()


def test_tiny_budget_degrades_to_a_short_notice():
    """An unusable budget still yields one honest line, never empty text."""
    conn = connect_for_tests()
    try:
        for index in range(30):
            _insert_fact(conn, f"f{index}", f"属性{index}", "值", created_at=index)
        md = _md(conn, max_tokens=1)

        assert md.strip()            # never empty: empty would read as "no read"
        assert "省略" in md
        assert "memory_recall" in md
        assert len(md.splitlines()) == 1
    finally:
        conn.close()


def test_tight_budget_shrinks_tail_sections_first():
    """Durable knowledge survives a squeeze; the tail is what gets trimmed."""
    conn = connect_for_tests()
    try:
        for index in range(30):
            _insert_fact(
                conn, f"e{index}", f"属性{index}", "值" * 10,
                memory_type="semantic", created_at=500 + index,
            )
        _insert_fact(
            conn, "d1", "决定", "先回滚再排查",
            memory_type="decision_rule", created_at=100,
        )
        md = _md(conn, max_tokens=200)

        assert "先回滚再排查" in md
        assert "决策规则" in md.splitlines()
        assert "已省略" in md
        # The durable section comes first, and the tail was trimmed hard.
        body = md.splitlines()
        assert body.index("决策规则") < body.index("属性")
        assert md.count("- 属性") < 30
        assert estimate_tokens(md) <= 200
    finally:
        conn.close()


# ---- a tight budget must keep what is important *and* recent -----------------


def test_fresh_fact_outranks_a_stale_higher_rank_fact():
    """近期是一等维度：陈旧的决策规则不再无条件压过最新的事实。

    Importance stays primary for modest age gaps (see the test below); this
    pins the other end of the trade — after ~8 half-lives of staleness, a fresh
    fact is worth more to the reader than a durable rule nobody has touched for
    months, and the render order says so.
    """
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "d1", "决定", "先回滚再排查",
            memory_type="decision_rule", created_at=1000,
        )
        _insert_fact(
            conn, "f1", "部署路径", "D:\\dsh",
            memory_type="semantic", created_at=1000 + 8 * HALF_LIFE,
        )
        md = _md(conn)
        assert md.index("D:\\dsh") < md.index("先回滚再排查")
    finally:
        conn.close()


def test_modest_staleness_does_not_flip_the_type_rank():
    """重要度仍是主信号：仅陈旧一倍半衰期不足以让 durable 知识退位。"""
    conn = connect_for_tests()
    try:
        _insert_fact(
            conn, "d1", "决定", "先回滚再排查",
            memory_type="decision_rule", created_at=1000,
        )
        _insert_fact(
            conn, "f1", "部署路径", "D:\\dsh",
            memory_type="semantic", created_at=1000 + HALF_LIFE,
        )
        md = _md(conn)
        assert md.index("先回滚再排查") < md.index("D:\\dsh")
    finally:
        conn.close()


def test_tight_budget_keeps_the_newest_fact_of_the_tail_section():
    """预算收紧时最新的一条必须活下来，而不是随所在分组被整段砍掉。

    Regression: the previous trim walked sections from the tail inward and
    emptied one before touching the next. The 属性 section outranks 事件 by type,
    so a squeeze used to clear the 事件 section wholesale — even though the event
    recorded minutes ago scores above every stale attribute. The budget is now
    spent globally, best score first.
    """
    conn = connect_for_tests()
    try:
        for index in range(30):
            _insert_fact(
                conn, f"a{index}", f"属性{index}", "值" * 10,
                memory_type="semantic", created_at=1000 + index,
            )
        _insert_fact(
            conn, "e1", "事件", "刚刚完成了插件部署",
            memory_type="episodic", created_at=1000 + 2 * HALF_LIFE,
        )
        md = _md(conn, max_tokens=150)

        assert "刚刚完成了插件部署" in md
        assert estimate_tokens(md) <= 150
    finally:
        conn.close()


def test_tight_budget_drops_the_stalest_lines_of_a_kept_section():
    """同类型事实按新旧取舍：最新的留下，最陈旧的让出预算。"""
    conn = connect_for_tests()
    try:
        for index in range(20):
            _insert_fact(
                conn, f"l{index}", "教训", f"教训正文{index}",
                memory_type="lesson", created_at=1000 + index * HALF_LIFE,
            )
        # The whole set costs ~111 tokens, so 100 forces a real trade.
        md = _md(conn, max_tokens=100)

        # The newest lesson is present, the oldest ones are gone.
        assert "教训正文19" in md
        assert "教训正文0" not in md
        assert "已省略" in md
        assert estimate_tokens(md) <= 100
    finally:
        conn.close()


def test_the_newest_survives_at_every_budget_down_to_one_line():
    """无论预算压到多小，最新的一条都是最后被放弃的。"""
    conn = connect_for_tests()
    try:
        for index in range(20):
            _insert_fact(
                conn, f"l{index}", "教训", f"教训正文{index}",
                memory_type="lesson", created_at=1000 + index * HALF_LIFE,
            )
        for budget in (120, 100, 80, 60, 40):
            md = _md(conn, max_tokens=budget)
            assert "教训正文19" in md, budget
            assert estimate_tokens(md) <= budget, budget
    finally:
        conn.close()


def test_tight_budget_never_leaves_a_section_label_without_content():
    """紧预算下也不渲染空的分组标签（旧实现同样保证，这里钉住）。"""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "d1", "决定", "先回滚再排查", memory_type="decision_rule")
        for index in range(30):
            _insert_fact(
                conn, f"a{index}", f"属性{index}", "值" * 10,
                memory_type="semantic", created_at=1000 + index,
            )
        md = _md(conn, max_tokens=100)
        lines = md.splitlines()
        titles = ("决策规则", "教训", "流程（SOP）", "流程", "偏好", "属性", "示例", "事件")
        for index, line in enumerate(lines[:-1]):
            if line in titles:
                assert lines[index + 1].startswith("- "), line
    finally:
        conn.close()


# ---- edge cases --------------------------------------------------------------


def test_empty_memory_notice_in_both_depths():
    """An empty store renders a non-blank notice, so callers can detect it."""
    conn = connect_for_tests()
    try:
        compact = _md(conn)
        detail = _md(conn, detail=True)
        assert compact.strip()
        assert "暂无" in compact
        assert "暂无" in detail
        assert not compact.startswith("# 记忆")
    finally:
        conn.close()


def test_facts_are_isolated_per_user():
    """Another user's facts never leak into a render."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "职业", "工程师", user_id="u1")
        assert "暂无" in _md(conn, user_id="u2")
    finally:
        conn.close()


def test_retracted_facts_are_excluded():
    """Soft-deleted facts do not appear in either depth."""
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "职业", "工程师")
        conn.execute("UPDATE facts SET status='retracted' WHERE fact_id='f1'")
        conn.commit()
        assert "暂无" in _md(conn)
        assert "工程师" not in _md(conn, detail=True)
    finally:
        conn.close()


def test_compact_is_meaningfully_smaller_than_detail():
    """The whole point: the injected view is much cheaper than the full list."""
    conn = connect_for_tests()
    try:
        for index in range(20):
            _insert_fact(
                conn, f"f{index}", f"属性{index}", "值" * 12,
                memory_type="semantic",
                content="一段不算短的知识正文" * 6,
                created_at=1000 + index,
            )
        compact = _md(conn, max_tokens=4000)
        detail = _md(conn, max_tokens=4000, detail=True)
        assert estimate_tokens(compact) < estimate_tokens(detail)
    finally:
        conn.close()
