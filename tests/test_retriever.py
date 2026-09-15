"""Tests for the retrieval pipeline (retriever.py): FTS + vector + RRF +
re-ranking + token budgeting, together with user-scoped recall semantics."""

from __future__ import annotations

import asyncio

from atom_memory.config import MemConfig
from atom_memory.db import connect_for_tests
from atom_memory.embedder import serialize_float32
from atom_memory.retriever import (
    Retriever,
    SOURCE_CREDIBILITY,
    estimate_tokens,
    rrf_merge,
    segment_text,
)

DIM = 512


def _vec(fill: float) -> bytes:
    return serialize_float32([fill] * DIM)


def _insert_fact(
    conn,
    fact_id: str,
    user_id: str,
    subject: str,
    predicate: str,
    obj: str,
    status: str = "active",
    source_type: str = "user_explicit",
    importance: float = 0.6,
):
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, source_type, status, observed_at, "
        "created_at, version) VALUES (?, ?, ?, ?, ?, ?, 0.8, ?, ?, ?, 1, 2, 1)",
        (
            fact_id, user_id, "s_test", subject, predicate, obj,
            importance, source_type, status,
        ),
    )
    conn.execute(
        "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
        (fact_id, " ".join(segment_text(f"{subject} {predicate} {obj}"))),
    )
    conn.execute(
        "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
        (fact_id, _vec(1.0)),
    )


class _FakeEmbed:
    """A fake embedder: every query gets the same vector, so vector KNN picks
    up facts whose vectors we inserted with the same value."""

    def __init__(self) -> None:
        self.calls = 0

    def embed_one(self, text: str) -> bytes:
        self.calls += 1
        return _vec(1.0)


# ---- RRF --------------------------------------------------------------------

def test_rrf_merge_ranks_overlap_higher():
    fts = ["a", "b", "c"]
    vec = ["b", "d", "e"]
    merged = rrf_merge(fts, vec, k=60)
    ids = [x[0] for x in merged]
    # "b" appears in both lists, so it should rank first.
    assert ids[0] == "b"
    # scores strictly descending
    scores = [s for _, s in merged]
    assert scores == sorted(scores, reverse=True)


def test_rrf_merge_preserves_single_list_order():
    merged = rrf_merge(["x", "y"], [], k=60)
    assert [x[0] for x in merged] == ["x", "y"]


def test_source_credibility_table():
    assert SOURCE_CREDIBILITY["user_explicit"] == 1.00
    assert SOURCE_CREDIBILITY["user_confirmed"] == 0.95
    assert SOURCE_CREDIBILITY["model_generated"] == 0.30


# ---- token estimate ----------------------------------------------------------

def test_estimate_tokens_basic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界") == 4  # 4 CJK chars
    assert estimate_tokens("hello") >= 1


# ---- retrieval primitives (vector / fts) -------------------------------------

def test_vector_knn_filters_user_and_status():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        _insert_fact(conn, "f2", "u1", "用户", "偏好", "加糖")
        _insert_fact(conn, "f3", "u1", "用户", "偏好", "奶茶", status="retracted")
        _insert_fact(conn, "f4", "u2", "用户", "偏好", "其他用户的")
        conn.commit()

        r = Retriever(conn, _FakeEmbed().embed_one)
        ids = r._vector_knn("u1", _vec(1.0), 10)
        assert set(ids) == {"f1", "f2"}  # only active u1 facts
    finally:
        conn.close()


def test_fts_search_returns_row():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        conn.commit()
        r = Retriever(conn, _FakeEmbed().embed_one)
        ids = r._fts_search("u1", "黑咖啡", 10)
        assert "f1" in ids
    finally:
        conn.close()


# ---- full search pipeline ------------------------------------------------------

def _search(conn, user_id, query, embed, **kw):
    retriever = Retriever(conn, embed.embed_one)
    return asyncio.run(retriever.search(user_id, query, **kw))


def test_search_returns_ranked_facts_with_final_score():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        _insert_fact(conn, "f2", "u1", "用户", "偏好", "加糖")
        conn.commit()
        embed = _FakeEmbed()
        facts = _search(conn, "u1", "咖啡", embed, top_k=10)
        assert len(facts) == 2
        ids = {f["fact_id"] for f in facts}
        assert ids == {"f1", "f2"}
        for f in facts:
            assert "final_score" in f
            assert f["status"] == "active"
        # sorted descending by final_score
        scores = [f["final_score"] for f in facts]
        assert scores == sorted(scores, reverse=True)
    finally:
        conn.close()


def test_search_isolates_user():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        _insert_fact(conn, "f2", "u2", "用户", "偏好", "黑咖啡")
        conn.commit()
        embed = _FakeEmbed()
        facts = _search(conn, "u1", "咖啡", embed, top_k=10)
        assert [f["fact_id"] for f in facts] == ["f1"]
    finally:
        conn.close()


def test_search_excludes_inactive():
    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        _insert_fact(conn, "f2", "u1", "用户", "偏好", "旧奶茶", status="superseded")
        conn.commit()
        embed = _FakeEmbed()
        facts = _search(conn, "u1", "咖啡", embed, top_k=10)
        assert [f["fact_id"] for f in facts] == ["f1"]
    finally:
        conn.close()


def test_search_empty_query_returns_empty():
    conn = connect_for_tests()
    try:
        embed = _FakeEmbed()
        facts = _search(conn, "u1", "   ", embed, top_k=10)
        assert facts == []
    finally:
        conn.close()


# ---- rerank formula -----------------------------------------------------------

def test_rerank_ties_break_by_confidence():
    conn = connect_for_tests()
    try:
        # Two facts with identical importance/recency; higher-confidence one
        # should win via the trust term.
        _insert_fact(conn, "f_high", "u1", "用户", "偏好", "黑咖啡")
        conn.execute(
            "UPDATE facts SET confidence = 0.99 WHERE fact_id = 'f_high'"
        )
        _insert_fact(conn, "f_low", "u1", "用户", "偏好", "加糖")
        conn.execute(
            "UPDATE facts SET confidence = 0.4 WHERE fact_id = 'f_low'"
        )
        conn.commit()
        embed = _FakeEmbed()
        facts = _search(conn, "u1", "咖啡", embed, top_k=10)
        assert facts[0]["fact_id"] == "f_high"
    finally:
        conn.close()


# ---- summary / profile rendering (derived views) -----------------------------

def test_summary_contains_fact_id():
    from atom_memory.summary import generate_summary

    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        conn.commit()
        md = generate_summary(conn, "u1", max_tokens=2000, detail=True)
        assert "f1" in md
        assert "黑咖啡" in md
        # other user not shown
        md2 = generate_summary(conn, "u2", max_tokens=2000, detail=True)
        assert "暂无" in md2
    finally:
        conn.close()


def test_summary_compact_omits_fact_id():
    """The injected depth drops the UUIDs, keeping the content itself."""
    from atom_memory.summary import generate_summary

    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        conn.commit()
        md = generate_summary(conn, "u1", max_tokens=2000, detail=False)
        assert "黑咖啡" in md
        assert "f1" not in md
    finally:
        conn.close()


def test_upsert_profile_source_priority():
    from atom_memory.profile import profile_md, upsert_profile

    conn = connect_for_tests()
    try:
        # weak source first, then strong source -> strong wins
        ok1 = upsert_profile(
            conn, "u1", "职业", "value", "工程师",
            source="system_inferred_low", confidence=0.5,
        )
        ok2 = upsert_profile(
            conn, "u1", "职业", "value", "科学家",
            source="user_explicit", confidence=0.9,
        )
        assert ok1 and ok2
        row = conn.execute(
            "SELECT value FROM user_profile WHERE user_id='u1' AND section='职业'"
        ).fetchone()
        assert row["value"] == "科学家"
        # strong source already present -> weaker source must not downgrade
        ok3 = upsert_profile(
            conn, "u1", "职业", "value", "教师",
            source="model_generated", confidence=0.3,
        )
        assert ok3 is False
        row = conn.execute(
            "SELECT value FROM user_profile WHERE user_id='u1' AND section='职业'"
        ).fetchone()
        assert row["value"] == "科学家"  # unchanged
        md = profile_md(conn, "u1", max_tokens=2000)
        assert "科学家" in md
    finally:
        conn.close()


def test_pinned_profile_row_blocks_automatic_writes():
    """A pinned row is frozen: derived writes may not update or replace it.

    Only a write that carries the pin explicitly (the settings panel toggling
    it) gets through, which is what makes the flag releasable at all.
    """
    from atom_memory.profile import upsert_profile

    conn = connect_for_tests()
    try:
        assert upsert_profile(
            conn, "u1", "职业", "value", "工程师",
            source="user_explicit", confidence=0.9,
        )
        # The panel's write path: value plus the pin itself.
        assert upsert_profile(
            conn, "u1", "职业", "value", "工程师",
            source="user_explicit", confidence=0.9, pinned=True,
        )
        assert conn.execute(
            "SELECT pinned FROM user_profile WHERE user_id='u1'"
        ).fetchone()["pinned"] == 1

        # A derived write of equal (or higher) authority would normally win —
        # the pin is what stops it, not the source ranking.
        assert upsert_profile(
            conn, "u1", "职业", "value", "产品经理",
            source="user_explicit", confidence=0.9,
        ) is False
        assert conn.execute(
            "SELECT value FROM user_profile WHERE user_id='u1'"
        ).fetchone()["value"] == "工程师"

        # Unpinning releases the row back to the normal rules.
        assert upsert_profile(
            conn, "u1", "职业", "value", "工程师",
            source="user_explicit", confidence=0.9, pinned=False,
        )
        assert upsert_profile(
            conn, "u1", "职业", "value", "产品经理",
            source="user_explicit", confidence=0.9,
        )
        row = conn.execute(
            "SELECT value, pinned FROM user_profile WHERE user_id='u1'"
        ).fetchone()
        assert row["value"] == "产品经理"
        assert row["pinned"] == 0
    finally:
        conn.close()


def test_derive_profile_from_facts_single_and_multi():
    from atom_memory.profile import derive_profile_from_facts

    conn = connect_for_tests()
    try:
        _insert_fact(conn, "f1", "u1", "用户", "偏好", "黑咖啡")
        _insert_fact(conn, "f2", "u1", "用户", "职业", "工程师")
        conn.commit()
        n = derive_profile_from_facts(conn, "u1")
        assert n >= 2
        prefs = conn.execute(
            "SELECT section, key, value FROM user_profile "
            "WHERE user_id='u1' AND section='偏好'"
        ).fetchall()
        assert any(r["key"] == "黑咖啡" and r["value"] == "喜欢" for r in prefs)
        prof = conn.execute(
            "SELECT value FROM user_profile WHERE user_id='u1' AND section='职业'"
        ).fetchone()
        assert prof["value"] == "工程师"
    finally:
        conn.close()

