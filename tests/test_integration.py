"""End-to-end integration tests through the public ``AtomMem`` API.

Covers the full pipeline (spec 阶段 4):

    add -> worker -> facts 落库
    recall 召回
    replace -> 旧 fact superseded + superseded_by 指向新 fact
    forget -> fact retracted
    summary 生成
    幂等：同 candidate 不重复写
"""

from __future__ import annotations

import asyncio

import pytest

from atom_memory import AtomMem, MemConfig
from atom_memory.embedder import serialize_float32


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


# ---- summary ----------------------------------------------------------------------

def test_summary_generation(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.8)
        md = await mem.summary("u1", detail=True)
        await mem.stop()
        return md

    md = _run(scenario())
    assert "黑咖啡" in md
    # fact_id present in the detail depth (bracketed id + footer)
    assert "fact_id" in md or "[f" in md


def test_summary_compact_has_no_fact_id(tmp_path, monkeypatch):
    """The injected depth renders the same content without the UUID payload."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.8)
        md = await mem.summary("u1", max_tokens=600, detail=False)
        await mem.stop()
        return md

    md = _run(scenario())
    assert "黑咖啡" in md
    assert "fact_id" not in md
    assert not md.splitlines()[0].startswith("# 记忆")


# ---- knowledge categories end-to-end (type + content + recall + summary) -------------

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


def test_summary_renders_knowledge_content(tmp_path, monkeypatch):
    """summary renders the knowledge content sub-line for a lesson fact."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "这次的教训是不能在没测试的情况下直接上线", turn_id=1)
        await asyncio.sleep(0.8)
        md = await mem.summary("u1")
        await mem.stop()
        return md

    md = _run(scenario())
    assert "不能在没测试的情况下直接上线" in md


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


# ---- recall budgets the knowledge body, not just the SPO title -----------------------

def test_recall_budgets_knowledge_content(tmp_path, monkeypatch):
    """A long knowledge body counts toward the recall token budget."""
    from atom_memory.extractor import _candidate_from_dict

    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        long_body = "长知识正文。" * 400  # ~2400 chars, far over a small budget
        for predicate, obj, ftype, content in (
            ("名字", "小强哥", "semantic", None),
            ("偏好", "黑咖啡", "semantic", None),
            ("运维手册", "部署流程", "sop", long_body),
        ):
            cand = _candidate_from_dict(
                {
                    "subject": "用户", "predicate": predicate, "object": obj,
                    "type": ftype, "content": content,
                    "confidence": 0.7, "importance": 0.5,
                },
                "u1", "s1", 0,
            )
            await mem._worker._persist_fact(cand, None)
        small = await mem.recall("u1", "用户", token_budget=100, top_k=10)
        big = await mem.recall("u1", "用户", token_budget=100000, top_k=10)
        await mem.stop()
        return small, big

    small, big = _run(scenario())
    # The three SPO lines alone are ~21 tokens, so before content was counted
    # every one of them fitted a 100-token budget. Now the body dominates.
    assert len(small["facts"]) < len(big["facts"])
    assert len(big["facts"]) == 3

