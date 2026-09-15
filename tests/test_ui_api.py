"""Tests for the UI-facing edit / backup / restore surface of AtomMem.

These methods back the dsh-atom-memory settings panel: paginated fact listing,
direct fact editing (with FTS/vector resync), profile row upsert/delete, and
JSON backup/restore with replace semantics. They deliberately use the fake
embedder so nothing touches model inference.
"""

from __future__ import annotations

import asyncio

import pytest

from atom_memory import AtomMem, MemConfig
from atom_memory.backup import BACKUP_VERSION, validate_backup
from atom_memory.embedder import serialize_float32


class _FakeEmbedder:
    def __init__(self, **kwargs) -> None:
        pass

    def embed_one(self, text: str) -> bytes:
        return serialize_float32([0.25] * 512)


def _make(tmp_path, monkeypatch) -> AtomMem:
    monkeypatch.setattr("atom_memory.api.Embedder", _FakeEmbedder)
    return AtomMem(
        MemConfig(
            db_path=str(tmp_path / "mem.db"),
            worker_poll_interval_sec=0.05,
            max_retries=3,
        )
    )


def _run(coro):
    return asyncio.run(coro)


def _insert_fact(mem, fact_id, subject, predicate, obj, user="u1", type="semantic", created_at=2):
    mem.db.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, source_type, status, observed_at, "
        "created_at, version, type) VALUES (?, ?, 's1', ?, ?, ?, 0.8, 0.6, "
        "'user_explicit', 'active', 1, ?, 1, ?)",
        (fact_id, user, subject, predicate, obj, created_at, type),
    )
    mem.db.commit()


def test_list_facts_paginates(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师", created_at=1)
        _insert_fact(mem, "f2", "用户", "偏好", "黑咖啡", created_at=2)

        page = mem.list_facts("u1", limit=1, offset=0)
        assert page["total"] == 2
        assert len(page["facts"]) == 1
        # Descending recency: newest (f2, created_at=2) first.
        assert page["facts"][0]["fact_id"] == "f2"

        page2 = mem.list_facts("u1", limit=1, offset=1)
        assert page2["facts"][0]["fact_id"] == "f1"

        # Retracted facts hidden by default.
        mem.db.execute("UPDATE facts SET status='retracted' WHERE fact_id='f2'")
        mem.db.commit()
        assert mem.list_facts("u1")["total"] == 1
        assert mem.list_facts("u1", include_retracted=True)["total"] == 2
        await mem.stop()

    _run(scenario())


def test_edit_fact_updates_spo_and_vectors(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师")

        updated = await mem.edit_fact("u1", "f1", object="产品经理")
        assert updated["object"] == "产品经理"
        assert updated["subject"] == "用户"

        # FTS/vector resynced to the new text.
        found = await mem.recall("u1", "产品经理", token_budget=2000)
        assert any(f["fact_id"] == "f1" for f in found["facts"])

        with pytest.raises(ValueError):
            await mem.edit_fact("u1", "missing", object="x")
        await mem.stop()

    _run(scenario())


def test_profile_upsert_delete(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        mem.upsert_profile("u1", "职业", "value", "工程师")
        rows = mem.list_profile("u1")["profile"]
        assert len(rows) == 1
        assert rows[0]["section"] == "职业"
        assert rows[0]["source"] == "user_explicit"
        # Rows are live by default: the 固定 flag must be opt-in.
        assert rows[0]["pinned"] is False

        mem.upsert_profile("u1", "职业", "value", "产品经理")
        assert mem.list_profile("u1")["profile"][0]["value"] == "产品经理"

        # The panel can pin an existing row, and the new state is reported back.
        assert mem.upsert_profile("u1", "职业", "value", "产品经理", pinned=True)["pinned"] is True
        assert mem.list_profile("u1")["profile"][0]["pinned"] is True
        # ...and an edit that omits the flag keeps the pin rather than dropping it.
        mem.upsert_profile("u1", "职业", "value", "架构师")
        assert mem.list_profile("u1")["profile"][0]["pinned"] is True

        res = mem.delete_profile("u1", "职业", "value")
        assert res["deleted"] == 1
        assert mem.list_profile("u1")["profile"] == []
        await mem.stop()

    _run(scenario())


def test_pinned_profile_row_survives_the_facts_projection(tmp_path, monkeypatch):
    """A 固定 profile row is never updated or replaced by memory itself.

    The profile is a projection over active facts, so a newer contradicting fact
    would normally rewrite the row (same source authority → write wins). The pin
    is what makes the row the user's, not the pipeline's — while the user's own
    edit through the panel still works, or the pin could never be corrected or
    released.
    """
    from atom_memory.profile import derive_profile_from_facts

    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师", created_at=1)
        derive_profile_from_facts(mem.db, "u1")
        assert mem.list_profile("u1")["profile"][0]["value"] == "工程师"

        mem.upsert_profile("u1", "职业", "value", "工程师", pinned=True)

        # A newer fact that contradicts the pinned row must not get through.
        _insert_fact(mem, "f2", "用户", "职业", "产品经理", created_at=2)
        derive_profile_from_facts(mem.db, "u1")
        row = mem.list_profile("u1")["profile"][0]
        assert row["value"] == "工程师"
        assert row["pinned"] is True

        # user_md re-derives before rendering; the pinned value is what renders,
        # and it is marked as fixed for the reader.
        md = await mem.user_md("u1")
        assert "工程师" in md
        assert "固定" in md
        assert "产品经理" not in md

        # The user's own edit still applies (that is the pin's escape hatch).
        mem.upsert_profile("u1", "职业", "value", "算法工程师", pinned=True)
        assert mem.list_profile("u1")["profile"][0]["value"] == "算法工程师"

        # Releasing the pin hands the row back to the pipeline.
        mem.upsert_profile("u1", "职业", "value", "算法工程师", pinned=False)
        derive_profile_from_facts(mem.db, "u1")
        assert mem.list_profile("u1")["profile"][0]["value"] == "产品经理"
        await mem.stop()

    _run(scenario())


def test_backup_restore_roundtrip(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师")
        _insert_fact(mem, "f2", "用户", "偏好", "黑咖啡")
        mem.upsert_profile("u1", "职业", "value", "工程师")

        snapshot = mem.backup("u1")
        assert snapshot["version"] == BACKUP_VERSION
        assert len(snapshot["facts"]) == 2
        assert len(snapshot["profile"]) == 1

        # Change memory, then restore the snapshot back (replace semantics).
        mem.db.execute("UPDATE facts SET status='retracted' WHERE user_id='u1'")
        mem.db.execute("DELETE FROM user_profile WHERE user_id='u1'")
        mem.db.commit()

        validate_backup(snapshot)
        restored = await mem.restore("u1", snapshot)
        assert restored["facts_written"] == 2
        assert mem.list_facts("u1")["total"] == 2
        assert len(mem.list_profile("u1")["profile"]) == 1
        await mem.stop()

    _run(scenario())


def test_backup_roundtrip_preserves_the_pin(tmp_path, monkeypatch):
    """A restore must not silently unfreeze a profile row the user pinned."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        mem.upsert_profile("u1", "职业", "value", "工程师", pinned=True)
        snapshot = mem.backup("u1")
        assert snapshot["profile"][0]["pinned"] == 1

        mem.db.execute("DELETE FROM user_profile WHERE user_id='u1'")
        mem.db.commit()
        await mem.restore("u1", snapshot)

        rows = mem.list_profile("u1")["profile"]
        assert rows[0]["value"] == "工程师"
        assert rows[0]["pinned"] is True

        # A snapshot taken before the pin existed restores as unpinned.
        for row in snapshot["profile"]:
            row.pop("pinned")
        mem.db.execute("DELETE FROM user_profile WHERE user_id='u1'")
        mem.db.commit()
        await mem.restore("u1", snapshot)
        assert mem.list_profile("u1")["profile"][0]["pinned"] is False
        await mem.stop()

    _run(scenario())


def test_export_memory_is_valueless_without_rows(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        snapshot = mem.backup("u1")
        assert snapshot["facts"] == []
        assert snapshot["profile"] == []
        await mem.stop()

    _run(scenario())


def test_validate_backup_rejects_bad_shape():
    with pytest.raises(ValueError):
        validate_backup({"version": 999, "facts": [], "profile": []})
    with pytest.raises(ValueError):
        validate_backup({"version": BACKUP_VERSION, "facts": "nope", "profile": []})


def test_recall_reports_single_valued_conflicts(tmp_path, monkeypatch):
    """recall() surfaces contradictory active facts under a single-valued
    predicate, instead of the old hardcoded empty ``conflicts`` stub."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        # Same (用户, 职业) — single-valued attribute — with two different
        # objects: a contradiction recall should report.
        _insert_fact(mem, "f_a", "用户", "职业", "工程师", created_at=1)
        _insert_fact(mem, "f_b", "用户", "职业", "设计师", created_at=2)
        # A multi-valued preference with two objects is NOT a conflict.
        _insert_fact(mem, "f_like1", "用户", "偏好", "黑咖啡")
        _insert_fact(mem, "f_like2", "用户", "偏好", "绿茶")

        r = await mem.recall("u1", "职业", token_budget=2000)
        conflicts = r["conflicts"]
        assert len(conflicts) == 1, "one contradictory pair reported"
        pair = conflicts[0]
        assert pair["subject"] == "用户"
        assert pair["predicate"] == "职业"
        assert {pair["left"], pair["right"]} == {"f_a", "f_b"}
        assert {pair["object_left"], pair["object_right"]} == {"工程师", "设计师"}

        # The preference objects must not have leaked into conflicts.
        csub = {(c["left"], c["right"]) for c in conflicts}
        assert ("f_like1", "f_like2") not in csub
        await mem.stop()

    _run(scenario())


def test_recall_conflicts_ignore_retracted_and_multi_valued(tmp_path, monkeypatch):
    """Conflicts only consider active rows, and only under single-valued keys."""
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        _insert_fact(mem, "f1", "用户", "职业", "工程师", created_at=1)
        _insert_fact(mem, "f2", "用户", "职业", "设计师", created_at=2)
        # Retracted duplicate must not be reported.
        _insert_fact(mem, "f_old", "用户", "职业", "经理", created_at=3)
        mem.db.execute("UPDATE facts SET status='retracted' WHERE fact_id='f_old'")
        # A knowledge (multi-valued) item sharing a predicate is independent.
        _insert_fact(mem, "f_k1", "用户", "发布流程", "A", type="sop")
        _insert_fact(mem, "f_k2", "用户", "发布流程", "B", type="sop")
        _insert_fact(mem, "f_ev1", "用户", "事件", "发布完成", type="episodic")
        _insert_fact(mem, "f_ev2", "用户", "事件", "回滚", type="episodic")
        mem.db.commit()

        r = await mem.recall("u1", "职业", token_budget=2000)
        pairs = {(c["left"], c["right"]) for c in r["conflicts"]}
        assert len(r["conflicts"]) == 1, "only the single-valued 职业 pair"
        assert ("f1", "f2") in pairs
        assert not any("f_old" in p for p in pairs)
        assert not any("发布流程" in c["predicate"] for c in r["conflicts"])
        assert not any("事件" in c["predicate"] for c in r["conflicts"])
        await mem.stop()

    _run(scenario())


def test_is_multi_valued_reflects_the_conflict_rule():
    from atom_memory.validator import is_multi_valued

    # Collection predicates and episodic/knowledge types are multi-valued.
    assert is_multi_valued("偏好", "semantic") is True
    assert is_multi_valued("职业", "episodic") is True
    assert is_multi_valued("职业", "sop") is True
    # An ordinary single-valued attribute is not.
    assert is_multi_valued("职业", "semantic") is False
