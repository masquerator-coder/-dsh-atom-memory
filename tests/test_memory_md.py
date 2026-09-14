"""Tests for ``memory.md`` rendering (``memory_md.py``).

Covers both render depths and, above all, the two properties the old
implementation got wrong: memory types stay distinct instead of collapsing into
one flat list, and ordering follows a real priority signal rather than plain
recency (``importance`` is only a signal when the extractor supplied one; the
neutral default defers to the type's rank).
"""

from __future__ import annotations

import re

from atom_memory.db import connect_for_tests
from atom_memory.models import NEUTRAL_SCORE
from atom_memory.memory_md import generate_memory_md
from atom_memory.retriever import estimate_tokens

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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
