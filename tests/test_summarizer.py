"""Tests for summarizer bucketing across semantic / procedural / episodic types."""

from __future__ import annotations

import json

from atom_memory.db import connect_for_tests
from atom_memory.summarizer import (
    rebuild_summary,
    try_rebuild,
    SCOPE_GLOBAL,
)


def _insert(conn, fact_id, predicate, obj, ftype, qualifiers=None):
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, qualifiers, type, observed_at, created_at) "
        "VALUES (?, 'u1', 's1', '用户', ?, ?, ?, ?, 1000, 1000)",
        (fact_id, predicate, obj, qualifiers, ftype),
    )


def _summary_text(conn, sid):
    return conn.execute(
        "SELECT text FROM summaries WHERE summary_id = ?", (sid,)
    ).fetchone()["text"]


def test_semantic_bucketing_preserved():
    """Attributes and preferences still render in their original forms."""
    conn = connect_for_tests()
    try:
        _insert(conn, "f1", "职业", "工程师", "semantic")
        _insert(conn, "f2", "偏好", "黑咖啡", "semantic")
        _insert(
            conn, "f3", "偏好", "加糖", "semantic",
            json.dumps({"negation": True}),
        )
        conn.commit()
        sid = rebuild_summary(conn, "u1")
        text = _summary_text(conn, sid)
        assert "职业: 工程师" in text
        assert "偏好 黑咖啡 (喜欢)" in text
        assert "偏好 加糖 (不喜欢)" in text
    finally:
        conn.close()


def test_procedural_renders_ordered_steps():
    """A procedural workflow renders as numbered, ordered steps."""
    conn = connect_for_tests()
    try:
        _insert(
            conn, "f4", "发布流程", "构建 -> 测试 -> 部署", "procedural",
            json.dumps({"steps": ["构建", "测试", "部署"]}),
        )
        conn.commit()
        sid = rebuild_summary(conn, "u1")
        text = _summary_text(conn, sid)
        assert "工作流程-发布流程:" in text
        assert "1)构建" in text and "2)测试" in text and "3)部署" in text
    finally:
        conn.close()


def test_episodic_renders_with_when():
    """An episodic event renders with its captured when qualifier."""
    conn = connect_for_tests()
    try:
        _insert(
            conn, "f5", "事件", "项目发布", "episodic",
            json.dumps({"when": "今天", "episodic": True}),
        )
        conn.commit()
        sid = rebuild_summary(conn, "u1")
        text = _summary_text(conn, sid)
        assert "[今天] 项目发布" in text
    finally:
        conn.close()


def test_all_three_types_coexist_in_one_summary():
    """All three buckets appear together in a single summary text."""
    conn = connect_for_tests()
    try:
        _insert(conn, "f1", "职业", "工程师", "semantic")
        _insert(
            conn, "f4", "发布流程", "构建 -> 测试 -> 部署", "procedural",
            json.dumps({"steps": ["构建", "测试", "部署"]}),
        )
        _insert(
            conn, "f5", "事件", "项目发布", "episodic",
            json.dumps({"when": "今天", "episodic": True}),
        )
        conn.commit()
        sid = rebuild_summary(conn, "u1")
        text = _summary_text(conn, sid)
        assert "职业: 工程师" in text
        assert "工作流程-发布流程:" in text and "1)构建" in text
        assert "[今天] 项目发布" in text
    finally:
        conn.close()


def test_procedural_without_steps_falls_back_to_object():
    """A procedural fact carrying no steps still renders (name fallback)."""
    conn = connect_for_tests()
    try:
        # object is plain text, no steps qualifier and no '->' separators.
        _insert(conn, "f6", "发布流程", "先做A再做B", "procedural", None)
        conn.commit()
        sid = rebuild_summary(conn, "u1")
        text = _summary_text(conn, sid)
        assert "工作流程-发布流程:" in text
        assert "先做A再做B" in text
    finally:
        conn.close()


def test_summary_reflects_type_of_retriever_ingested_facts():
    """memory_md summary path keeps working end to end (sanity)."""
    conn = connect_for_tests()
    try:
        assert rebuild_summary(conn, "u1") is None  # no facts yet
        _insert(conn, "f1", "事件", "安全演练", "episodic",
                json.dumps({"when": "上周"}))
        conn.commit()
        sid = rebuild_summary(conn, "u1")
        assert sid is not None
        text = _summary_text(conn, sid)
        assert "[上周] 安全演练" in text
    finally:
        conn.close()


def test_try_rebuild_picks_up_episodic_facts():
    """try_rebuild aggregates all active facts including events."""
    conn = connect_for_tests()
    try:
        _insert(conn, "f1", "事件", "故障复盘", "episodic",
                json.dumps({"when": "昨天"}))
        conn.commit()
        assert try_rebuild(conn, "u1", scope=SCOPE_GLOBAL, debounce_sec=0)
        text = conn.execute(
            "SELECT text FROM summaries WHERE user_id='u1'"
        ).fetchone()["text"]
        assert "昨天" in text and "故障复盘" in text
    finally:
        conn.close()


# ---- knowledge categories in summaries (light vs long-form) ------------------------

def test_light_knowledge_renders_in_summary():
    """decision_rule / lesson are light knowledge: rendered as pred: object."""
    conn = connect_for_tests()
    try:
        _insert(conn, "f9", "教训", "先备份再升级", "lesson")
        _insert(conn, "f10", "决策规则", "当出事故时先回滚", "decision_rule")
        conn.commit()
        sid = rebuild_summary(conn, "u1")
        text = _summary_text(conn, sid)
        assert "教训: 先备份再升级" in text
        assert "决策规则: 当出事故时先回滚" in text
    finally:
        conn.close()


def test_long_form_knowledge_excluded_from_summary_but_tracked():
    """sop / few_shot bodies are excluded from the text but their fact_ids tracked."""
    conn = connect_for_tests()
    try:
        _insert(conn, "f7", "发布SOP", "构建测试部署", "sop")
        _insert(conn, "f8", "Few-shot", "示例对话", "few_shot")
        conn.commit()
        sid = rebuild_summary(conn, "u1")
        assert sid is not None
        row = conn.execute(
            "SELECT text, fact_ids FROM summaries WHERE summary_id = ?", (sid,)
        ).fetchone()
        # long-form categories must not leak into the summary text
        assert "发布SOP" not in row["text"]
        assert "Few-shot" not in row["text"]
        # ...but their fact ids are still referenced for consistency
        assert "f7" in row["fact_ids"] and "f8" in row["fact_ids"]
    finally:
        conn.close()


def test_light_and_long_knowledge_coexist_with_semantic():
    """A mixed summary keeps semantic prefs, adds light knowledge, skips long-form."""
    conn = connect_for_tests()
    try:
        _insert(conn, "f1", "偏好", "黑咖啡", "semantic")
        _insert(conn, "f9", "教训", "先备份再升级", "lesson")
        _insert(conn, "f7", "发布SOP", "构建测试部署", "sop")
        conn.commit()
        sid = rebuild_summary(conn, "u1")
        text = _summary_text(conn, sid)
        assert "偏好 黑咖啡 (喜欢)" in text
        assert "教训: 先备份再升级" in text
        assert "发布SOP" not in text
    finally:
        conn.close()

