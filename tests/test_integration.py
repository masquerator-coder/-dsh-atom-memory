"""End-to-end integration tests through the public ``AtomMem`` API.

Covers the full pipeline (spec 阶段 4):

    add -> worker -> facts 落库
    recall 召回
    replace -> 旧 fact superseded + superseded_by 指向新 fact
    forget -> fact retracted
    memory_md 生成
    摘要 stale -> 重建
    幂等：同 candidate 不重复写
"""

from __future__ import annotations

import asyncio

import pytest

from atom_memory import AtomMem, MemConfig
from atom_memory.embedder import serialize_float32
from atom_memory.summarizer import SCOPE_GLOBAL, mark_stale, try_rebuild


class _FakeEmbedder:
    """Stand-in for the real FastEmbed model (returns a fixed vector)."""

    def __init__(self, **kwargs) -> None:
        # Accept (and ignore) the real embedder's construction args.
        pass

    def embed_one(self, text: str) -> bytes:
        return serialize_float32([0.5] * 512)


def _make(tmp_path, monkeypatch, **overrides) -> AtomMem:
    defaults = dict(
        db_path=str(tmp_path / "mem.db"),
        worker_poll_interval_sec=0.05,
        summary_rebuild_debounce_sec=0.0,
        max_retries=3,
    )
    defaults.update(overrides)
    # Swap the real embedder for a deterministic fake.
    monkeypatch.setattr("atom_memory.api.Embedder", _FakeEmbedder)
    return AtomMem(MemConfig(**defaults))


def _run(coro):
    return asyncio.run(coro)


async def _active_fact_ids(mem):
    return [
        r["fact_id"]
        for r in mem.db.execute(
            "SELECT fact_id FROM facts WHERE user_id='u1' AND status='active'"
        ).fetchall()
    ]


# ---- add -> worker -> facts 落库 ---------------------------------------------

def test_add_worker_persists_fact(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.8)
        row = mem.db.execute(
            "SELECT subject, predicate, object, status FROM facts WHERE user_id='u1'"
        ).fetchone()
        await mem.stop()
        return row

    row = _run(scenario())
    assert tuple(row) == ("用户", "偏好", "黑咖啡", "active")


# ---- recall --------------------------------------------------------------------

def test_recall_returns_fact(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.8)
        r = await mem.recall("u1", "咖啡", token_budget=2000)
        await mem.stop()
        return r

    r = _run(scenario())
    assert any(f["object"] == "黑咖啡" for f in r["facts"])
    assert r["token_count"] >= 0


# ---- replace ---------------------------------------------------------------------

def test_replace_supersedes_old(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.8)
        old_id = (await _active_fact_ids(mem))[0]

        await mem.replace("u1", old_id, "用户喜欢少糖黑咖啡")
        await asyncio.sleep(0.8)

        old = mem.db.execute(
            "SELECT status, superseded_by FROM facts WHERE fact_id = ?", (old_id,)
        ).fetchone()
        new = mem.db.execute(
            "SELECT subject, object, status FROM facts "
            "WHERE user_id='u1' AND fact_id = ?", (old["superseded_by"],)
        ).fetchone()
        await mem.stop()
        return old, new

    old, new = _run(scenario())
    assert old["status"] == "superseded"
    assert old["superseded_by"] is not None
    # superseded_by points at an active replacement fact
    assert new is not None and new["status"] == "active"
    assert new["object"] == "少糖黑咖啡"


# ---- forget -------------------------------------------------------------------------

def test_forget_retracts_fact(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.8)
        old_id = (await _active_fact_ids(mem))[0]

        await mem.forget("u1", fact_id=old_id)
        await asyncio.sleep(0.8)

        row = mem.db.execute(
            "SELECT status FROM facts WHERE fact_id = ?", (old_id,)
        ).fetchone()
        await mem.stop()
        return row

    row = _run(scenario())
    assert row["status"] == "retracted"


# ---- memory_md ------------------------------------------------------------------------

def test_memory_md_generation(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.8)
        md = await mem.memory_md("u1")
        await mem.stop()
        return md

    md = _run(scenario())
    assert "黑咖啡" in md
    # fact_id present (unique-reference footer or bracketed id)
    assert "fact_id" in md or "[f" in md


# ---- knowledge categories end-to-end (type + content + recall + memory_md) -------------

def test_knowledge_fact_persists_type_and_content(tmp_path, monkeypatch):
    """A lesson/SOP utterance persists its type discriminator and full content body."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "这次的教训是不能在没测试的情况下直接上线", turn_id=1)
        await mem.add("u1", "s1", "发布SOP是先构建再测试最后部署", turn_id=2)
        await mem.add("u1", "s1", "当线上出事故时应该先回滚再排查", turn_id=3)
        await asyncio.sleep(1.0)
        rows = mem.db.execute(
            "SELECT predicate, object, type, content FROM facts "
            "WHERE user_id='u1' AND status='active'"
        ).fetchall()
        await mem.stop()
        return {r["type"]: (r["predicate"], r["object"], r["content"]) for r in rows}

    kinds = _run(scenario())
    lesson = kinds.get("lesson")
    assert lesson is not None and lesson[1] == "不能在没测试的情况下直接上线"
    assert lesson[2] == "不能在没测试的情况下直接上线"

    sop = kinds.get("sop")
    assert sop is not None and "构建" in sop[2]

    dr = kinds.get("decision_rule")
    assert dr is not None and "回滚" in dr[2]


def test_recall_matches_knowledge_content(tmp_path, monkeypatch):
    """recall() returns knowledge facts with their type and content available."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "这次的教训是不能在没测试的情况下直接上线", turn_id=1)
        await asyncio.sleep(1.0)
        result = await mem.recall("u1", "上线前要测试", token_budget=2000)
        await mem.stop()
        return result

    result = _run(scenario())
    facts = result["facts"]
    assert len(facts) >= 1
    # recall returns the type and content alongside the fact
    recalled = {f["predicate"]: f for f in facts}
    lesson = recalled.get("教训")
    assert lesson is not None and lesson["type"] == "lesson"
    assert lesson.get("content") == "不能在没测试的情况下直接上线"


def test_memory_md_renders_knowledge_content(tmp_path, monkeypatch):
    """memory.md renders the knowledge content sub-line for a lesson fact."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "这次的教训是不能在没测试的情况下直接上线", turn_id=1)
        await asyncio.sleep(0.8)
        md = await mem.memory_md("u1")
        await mem.stop()
        return md

    md = _run(scenario())
    assert "不能在没测试的情况下直接上线" in md


# ---- summary stale -> rebuild ------------------------------------------------------------

def test_summary_is_rebuilt_after_mutation(tmp_path, monkeypatch):
    """A mutation marks the summary stale and the worker rebuilds it (version++)."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.8)
        v1 = mem.db.execute(
            "SELECT version FROM summaries WHERE user_id='u1'"
        ).fetchone()
        assert v1 is not None and v1["version"] >= 1

        # second mutation re-marks stale and triggers another (debounce=0) rebuild
        await mem.add("u1", "s1", "用户的职业是工程师", turn_id=2)
        await asyncio.sleep(0.8)
        row = mem.db.execute(
            "SELECT version, stale FROM summaries WHERE user_id='u1'"
        ).fetchone()
        # No stale rows remain (rebuild settled) and version advanced.
        stale_any = mem.db.execute(
            "SELECT COUNT(*) AS n FROM summaries WHERE user_id='u1' AND stale=1"
        ).fetchone()["n"]
        await mem.stop()
        return v1["version"], row, stale_any

    v1, row, stale_any = _run(scenario())
    assert row["version"] > v1  # rebuild ran again after the mutation
    assert row["stale"] == 0  # final summary is fresh
    assert stale_any == 0  # nothing left stale


def test_summarizer_stale_then_debounced_rebuild(tmp_path, monkeypatch):
    """Direct summarizer semantics: stale rows rebuild only after debounce."""
    from atom_memory.db import open_db

    conn = open_db(MemConfig(db_path=str(tmp_path / "sum.db")))

    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, status, observed_at, created_at) VALUES "
        "('f1','u1','s1','用户','偏好','黑咖啡','active',1,1)"
    )
    conn.commit()

    # build a fresh summary
    assert try_rebuild(conn, "u1", scope=SCOPE_GLOBAL, debounce_sec=0, max_tokens=1500)
    fresh = conn.execute(
        "SELECT stale FROM summaries WHERE user_id='u1'"
    ).fetchone()
    assert fresh["stale"] == 0

    # mark stale, queue a rebuild but block it with a long debounce
    mark_stale(conn, "u1")
    assert try_rebuild(conn, "u1", scope=SCOPE_GLOBAL, debounce_sec=3600, max_tokens=1500) is False
    row = conn.execute("SELECT stale FROM summaries WHERE user_id='u1'").fetchone()
    assert row["stale"] == 1  # still stale (deferred)

    # debounce elapsed -> rebuild -> fresh
    assert try_rebuild(conn, "u1", scope=SCOPE_GLOBAL, debounce_sec=0, max_tokens=1500)
    row = conn.execute("SELECT stale FROM summaries WHERE user_id='u1'").fetchone()
    assert row["stale"] == 0
    conn.close()


# ---- idempotency: 同 candidate 不重复写 -----------------------------------------------

def test_duplicate_utterance_not_written_twice(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.5)
        # same statement again: the duplicate is suppressed by validation
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=2)
        await asyncio.sleep(0.8)
        n = mem.db.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE user_id='u1' "
            "AND subject='用户' AND predicate='偏好' AND object='黑咖啡' "
            "AND status='active'"
        ).fetchone()["n"]
        await mem.stop()
        return n

    n = _run(scenario())
    assert n == 1  # exactly one active fact for the duplicated SPO


# ---- stats -----------------------------------------------------------------------------

def test_stats_counts(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.8)
        s = mem.stats("u1")
        await mem.stop()
        return s

    s = _run(scenario())
    assert s["facts"] >= 1
    assert s["pending"] == 0  # all candidates processed
    assert s["stale_summaries"] >= 0
    assert s["summaries"] >= 1  # a fresh summary was built


# ---- memory-type persistence: procedural / episodic through the API ----------------

def test_add_persists_procedural_and_episodic_types(tmp_path, monkeypatch):
    """Adding procedural and episodic utterances stores their type on facts."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "发布流程是1.构建 2.测试 3.部署", turn_id=1)
        await mem.add("u1", "s1", "今天完成了项目发布", turn_id=2)
        await asyncio.sleep(1.2)
        rows = mem.db.execute(
            "SELECT subject, predicate, object, type FROM facts WHERE user_id='u1' "
            "ORDER BY created_at"
        ).fetchall()
        await mem.stop()
        return rows

    rows = _run(scenario())
    types = {r["type"] for r in rows}
    assert "procedural" in types
    assert "episodic" in types
    proc = [r for r in rows if r["type"] == "procedural"]
    epis = [r for r in rows if r["type"] == "episodic"]
    assert proc and proc[0]["predicate"] == "发布流程"
    assert epis and epis[0]["predicate"] == "事件"


# ---- extraction precedence: LLM-first end to end -------------------------------------

def test_llm_first_suppresses_rules_through_worker(tmp_path, monkeypatch):
    """A configured LLM extractor's result is authoritative in the worker path."""
    llm_calls = {"n": 0}

    def fake_llm(text, user_id, session_id, turn_id):
        llm_calls["n"] += 1
        return [{"subject": "用户", "predicate": "项目", "object": "LLM抽取结果"}]

    mem = _make(tmp_path, monkeypatch, llm_extractor=fake_llm)

    async def scenario():
        await mem.start()
        # The text matches the 偏好 rule, but LLM-first must win.
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(1.0)
        rows = mem.db.execute(
            "SELECT subject, predicate, object FROM facts WHERE user_id='u1' "
            "AND status='active'"
        ).fetchall()
        await mem.stop()
        return rows

    rows = _run(scenario())
    assert llm_calls["n"] >= 1
    assert len(rows) == 1
    assert (rows[0]["predicate"], rows[0]["object"]) == ("项目", "LLM抽取结果")


def test_llm_failure_falls_back_to_rules_through_worker(tmp_path, monkeypatch):
    """A throwing LLM extractor degrades to rule extraction in the worker path."""
    def bad_llm(*args, **kwargs):
        raise RuntimeError("llm down")

    mem = _make(tmp_path, monkeypatch, llm_extractor=bad_llm)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(1.0)
        rows = mem.db.execute(
            "SELECT subject, predicate, object FROM facts WHERE user_id='u1' "
            "AND status='active'"
        ).fetchall()
        await mem.stop()
        return rows

    rows = _run(scenario())
    # LLM failed -> rule fallback persists the 偏好 fact, nothing is dropped.
    assert len(rows) == 1
    assert (rows[0]["predicate"], rows[0]["object"]) == ("偏好", "黑咖啡")

