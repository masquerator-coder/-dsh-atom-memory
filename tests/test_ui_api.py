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
            summary_rebuild_debounce_sec=0.0,
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

        mem.upsert_profile("u1", "职业", "value", "产品经理")
        assert mem.list_profile("u1")["profile"][0]["value"] == "产品经理"

        res = mem.delete_profile("u1", "职业", "value")
        assert res["deleted"] == 1
        assert mem.list_profile("u1")["profile"] == []
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


def test_export_memory_is_valueless_without_rows(tmp_path, monkeypatch):
    mem = _make(tmp_path, monkeypatch)

    async def scenario():
        await mem.start()
        snapshot = mem.backup("u1")
        assert snapshot["facts"] == []
        assert snapshot["profile"] == []
        assert snapshot["summaries"] == []
        await mem.stop()

    _run(scenario())


def test_validate_backup_rejects_bad_shape():
    with pytest.raises(ValueError):
        validate_backup({"version": 999, "facts": [], "profile": [], "summaries": []})
    with pytest.raises(ValueError):
        validate_backup({"version": BACKUP_VERSION, "facts": "nope", "profile": [], "summaries": []})
