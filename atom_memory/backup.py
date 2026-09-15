"""Backup and restore of a user's memory, as portable JSON.

A backup is a *lossy-but-faithful* snapshot of the derived views the user can
read and edit in the UI: the active atomic facts (with their SPO triples, type,
content body, importance and timestamps) and the user-profile rows. It never
carries credentials or other secrets — the library only stores facts and
derived views.

Restore uses "replace semantics": the target user's active facts and profile
rows are soft-retracted first, then the snapshot is written back as new
``user_explicit`` facts and profile rows. This keeps the restore deterministic
and avoids resurrecting stale duplicates against live memory, at the cost of
losing fact identity across a restore (ids are regenerated). The user's pinned
profile rows round-trip with their pin intact, so a restore cannot silently
unfreeze an attribute the user had declared fixed. It honours the
library's soft-delete invariant on the old rows and always stays committed
inside one transaction so a failed import leaves memory untouched.

@module atom_memory/backup
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from typing import Any, Callable, Dict, Optional

from .db import now_ms
from .retriever import segment_text

# Snapshot format version, bumped on any structurally breaking change.
#
# v2: the derived summary layer was removed entirely (summarizer.py + the
# `summaries` table were dropped in favour of the on-demand `summary` view), so
# backups no longer carry a `summaries` key. `validate_backup` requires the
# version to match exactly and rejects older snapshots (which still embed a
# `summaries` list from the deleted layer) instead of silently importing them.
BACKUP_VERSION = 2

# Column groups round-tripped through a snapshot. `user_id` is intentionally
# excluded from facts/profile: restore re-targets everything to the caller's
# `target_user_id`, so embeding the source owner would be wrong.
_FACT_KEYS = (
    "fact_id", "session_id", "subject", "predicate", "object",
    "qualifiers", "confidence", "importance", "privacy", "source_type",
    "type", "content", "observed_at", "created_at",
)
_PROFILE_KEYS = (
    "section", "key", "value", "source", "confidence", "privacy",
    "pinned", "updated_at",
)


def export_memory(conn: sqlite3.Connection, user_id: str) -> dict:
    """Serialize one user's active memory to a portable dict.

    Only ``active`` facts are exported (retracted / superseded are excluded).
    Serialization is deterministic (ordered queries), so two exports of
    unchanged memory compare equal.

    Args:
        conn: The SQLite connection.
        user_id: Owner of the memory to export.

    Returns:
        A dict shaped for ``json.dumps``:
        ``{"version", "exported_at", "user_id", "facts", "profile"}``.
    """
    fact_rows = conn.execute(
        "SELECT fact_id, session_id, subject, predicate, object, qualifiers, "
        "confidence, importance, privacy, source_type, type, content, "
        "observed_at, created_at FROM facts "
        "WHERE user_id = ? AND status = 'active' "
        "ORDER BY created_at ASC",
        (user_id,),
    ).fetchall()
    facts = [{k: r[k] for k in _FACT_KEYS} for r in fact_rows]

    profile_rows = conn.execute(
        "SELECT user_id, section, key, value, source, confidence, privacy, "
        "pinned, updated_at FROM user_profile WHERE user_id = ? ORDER BY section, key",
        (user_id,),
    ).fetchall()
    profile = [{k: r[k] for k in _PROFILE_KEYS} for r in profile_rows]

    return {
        "version": BACKUP_VERSION,
        "exported_at": now_ms(),
        "user_id": user_id,
        "facts": facts,
        "profile": profile,
    }


def validate_backup(payload: Any) -> Dict[str, Any]:
    """Validate a parsed backup payload, raising ValueError on a bad shape.

    Args:
        payload: The decoded JSON object handed to restore.

    Returns:
        The payload, narrowed to the expected dict shape for import.
    """
    if not isinstance(payload, dict):
        raise ValueError("backup payload must be a JSON object")
    version = payload.get("version")
    if version != BACKUP_VERSION:
        raise ValueError(
            f"unsupported backup version {version!r} (expected {BACKUP_VERSION})"
        )
    for key in ("facts", "profile"):
        if not isinstance(payload.get(key), list):
            raise ValueError(f"backup field {key!r} must be a list")
    return payload


async def import_memory(
    conn: sqlite3.Connection,
    target_user_id: str,
    payload: dict,
    embed_func: Callable[[str], bytes],
) -> Dict[str, Any]:
    """Import a snapshot into one user's memory with replace semantics.

    The target user's active facts are soft-retracted and their profile rows
    deleted within the same transaction as the insert, so a failed import
    (raised before commit) leaves memory untouched. Facts are re-embedded via
    ``embed_func``. The SQLite connection is used only on the caller's thread
    (SQLite is not thread-safe across threads), while the CPU-bound embedding
    calls run in worker threads — mirroring the library's own write path.

    **Atomicity across the async/worker boundary.** Every fact's embedding is
    computed *up front*, before the transaction opens. The ``AtomMem`` store and
    its worker share the single connection on the same event-loop thread, and
    the worker commits its own writes mid-task; had the transaction stayed open
    across an ``await asyncio.to_thread(embed, ...)`` (as this function once
    did), the worker's ``commit()`` would have committed this import's
    partial writes too — breaking the "failed import leaves memory untouched"
    guarantee. Precomputing all embeddings first makes the transaction purely
    synchronous, so no worker write can interleave with it.

    Args:
        conn: The SQLite connection (must stay on one thread).
        target_user_id: The user whose memory is replaced.
        payload: A validated backup dict from :func:`export_memory`.
        embed_func: ``callable(searchable_text) -> embedding_bytes``.

    Returns:
        ``{"facts_written", "profile_written"}`` counts.
    """
    user_id = target_user_id
    now = now_ms()

    # Precompute every fact's embedding off-thread, before any DB work. This is
    # the only place we await, so the transaction below never spans an await.
    prepared_facts: list = []
    for f in payload.get("facts", []):
        subject = str(f.get("subject", ""))
        predicate = str(f.get("predicate", ""))
        obj = str(f.get("object", ""))
        content = f.get("content") or None
        searchable = (f"{subject} {predicate} {obj} " + (content or "")).strip()
        embed_text = searchable or obj
        blob = await asyncio.to_thread(embed_func, embed_text)
        prepared_facts.append((f, blob))

    with conn:
        # Replace semantics: clear the user's live derived state first, then
        # write the snapshot. Soft-retract keeps provenance of what was cleared.
        conn.execute(
            "UPDATE facts SET status = 'retracted' WHERE user_id = ? "
            "AND status IN ('active', 'superseded')",
            (user_id,),
        )
        conn.execute(
            "DELETE FROM user_profile WHERE user_id = ?",
            (user_id,),
        )

        facts_written = 0
        for f, blob in prepared_facts:
            _write_fact(conn, user_id, f, blob, now)
            facts_written += 1

        profile_written = 0
        for p in payload.get("profile", []):
            section = str(p.get("section", ""))
            key = str(p.get("key", ""))
            value = str(p.get("value", ""))
            if not section or not key:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO user_profile(user_id, section, key, "
                "value, source, confidence, privacy, pinned, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id, section, key, value,
                    str(p.get("source", "user_explicit")),
                    float(p.get("confidence", 0.5)),
                    str(p.get("privacy", "private")),
                    # A snapshot from before the pin existed simply has no
                    # `pinned` key; such rows restore as unpinned.
                    1 if p.get("pinned") else 0,
                    int(p.get("updated_at", now)),
                ),
            )
            profile_written += 1

    return {"facts_written": facts_written, "profile_written": profile_written}


def _write_fact(
    conn: sqlite3.Connection,
    user_id: str,
    f: dict,
    blob: bytes,
    now: int,
) -> str:
    """Insert one snapshot fact as a fresh row mirroring the library writer.

    This mirrors the shape of ``worker._persist_fact`` (user_explicit source,
    active status, regenerated id, re-embedded vector) so imported facts are
    first-class entries. The embedding ``blob`` is supplied precomputed by the
    caller (outside the transaction) so this stays synchronous.
    """
    fact_id = str(uuid.uuid4())
    subject = str(f.get("subject", ""))
    predicate = str(f.get("predicate", ""))
    obj = str(f.get("object", ""))
    content = f.get("content") or None
    searchable = (f"{subject} {predicate} {obj} " + (content or "")).strip()

    session_id = str(f.get("session_id", "restore"))
    ftype = str(f.get("type", "semantic") or "semantic")
    observed_at = int(f.get("observed_at", now))
    created_at = int(f.get("created_at", now))

    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, qualifiers, confidence, importance, privacy, source_type, "
        "status, superseded_by, observed_at, created_at, trace_id, version, "
        "type, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "?, ?, ?, ?)",
        (
            fact_id, user_id, session_id, subject, predicate, obj,
            f.get("qualifiers"),
            float(f.get("confidence", 0.5)),
            float(f.get("importance", 0.5)),
            str(f.get("privacy", "private")),
            "user_explicit",
            "active",
            None,
            observed_at,
            created_at,
            None,
            1,
            ftype,
            content,
        ),
    )
    conn.execute(
        "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
        (fact_id, " ".join(segment_text(searchable))),
    )
    conn.execute(
        "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
        (fact_id, blob),
    )
    return fact_id
