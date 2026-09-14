"""Tests for the SQLite storage layer (db.py + migrations/001_init.sql)."""

from __future__ import annotations

from atom_memory.config import MemConfig
from atom_memory.db import SCHEMA_VERSION, connect_for_tests, open_db
from atom_memory.embedder import serialize_float32


def test_open_db_creates_all_tables(tmp_path):
    """Opening a fresh database must create every expected table."""
    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        names = {r[0] for r in rows}
    finally:
        conn.close()

    expected = {
        "facts",
        "fact_candidates",
        "summaries",
        "user_profile",
        "events",
        "task_queue",
        "facts_fts",
        "facts_vec",
    }
    assert expected.issubset(names)


def test_in_memory_db_also_migrates():
    """An in-memory connection runs the same migrations."""
    conn = connect_for_tests()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == SCHEMA_VERSION
    finally:
        conn.close()


def test_wal_mode_enabled(tmp_path):
    """The journal must be set to WAL (the connection reports it)."""
    cfg = MemConfig(db_path=str(tmp_path / "test.db"))
    conn = open_db(cfg)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_vec_extension_loaded():
    """The sqlite-vec extension must expose its version function."""
    conn = connect_for_tests()
    try:
        row = conn.execute("SELECT vec_version()").fetchone()
        assert row is not None and row[0]
    finally:
        conn.close()


def test_facts_vec_is_knn_queryable():
    """facts_vec must accept rows and answer a KNN query."""
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
            ("f1", serialize_float32([0.0] * 512)),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT fact_id FROM facts_vec "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT 5",
            (serialize_float32([1.0] * 512),),
        ).fetchall()
        assert rows, "KNN query returned no rows"
        assert rows[0]["fact_id"] == "f1"
    finally:
        conn.close()


def test_facts_fts_is_queryable():
    """facts_fts must accept rows and answer a MATCH query.

    NOTE: FTS5's unicode61 tokenizer treats a contiguous CJK span as a single
    token, so the match must target a whole token. Chinese word segmentation
    (via jieba) is the indexer's responsibility and is exercised elsewhere.
    """
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
            ("f1", "黑咖啡"),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT fact_id FROM facts_fts WHERE facts_fts MATCH ?",
            ("黑咖啡",),
        ).fetchall()
        assert rows, "FTS query returned no rows"
        assert rows[0]["fact_id"] == "f1"
    finally:
        conn.close()


def test_idempotency_key_is_unique(tmp_path):
    """The UNIQUE constraint on fact_candidates.idempotency_key is enforced."""
    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        sql = (
            "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
            "turn_id, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?)"
        )
        conn.execute(sql, ("c1", "u1", "s1", 0, "key-1", 1000))
        conn.commit()
        try:
            conn.execute(sql, ("c2", "u1", "s1", 0, "key-1", 1000))
            conn.commit()
            raised = False
        except Exception:
            conn.rollback()
            raised = True
        assert raised, "UNIQUE(idempotency_key) was not enforced"
    finally:
        conn.close()


def test_all_fact_columns_exist(tmp_path):
    """The facts table must expose every required column."""
    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()
        }
    finally:
        conn.close()

    expected = {
        "fact_id", "user_id", "session_id", "subject", "predicate", "object",
        "qualifiers", "confidence", "importance", "privacy", "source_type",
        "status", "superseded_by", "observed_at", "created_at", "trace_id",
        "version", "type", "content",
    }
    assert expected.issubset(cols)


def test_all_profile_columns_exist(tmp_path):
    """The user_profile table must expose every required column, `pinned`
    included — it is what freezes a row against automatic memory updates."""
    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(user_profile)").fetchall()
        }
    finally:
        conn.close()

    expected = {
        "user_id", "section", "key", "value", "source", "confidence",
        "privacy", "pinned", "updated_at",
    }
    assert expected.issubset(cols)


def test_pinned_column_defaults_to_unpinned(tmp_path):
    """A profile row written without a pin is not pinned."""
    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        conn.execute(
            "INSERT INTO user_profile(user_id, section, key, value, updated_at) "
            "VALUES ('u1', '职业', 'value', '工程师', 1000)"
        )
        conn.commit()
        row = conn.execute("SELECT pinned FROM user_profile").fetchone()
        assert row["pinned"] == 0
    finally:
        conn.close()


def test_type_column_defaults_semantic(tmp_path):
    """The new type column defaults to 'semantic' for untouched rows."""
    conn = open_db(MemConfig(db_path=str(tmp_path / "test.db")))
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, observed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("f1", "u1", "s1", "用户", "偏好", "黑咖啡", 1000, 1000),
        )
        conn.commit()
        row = conn.execute(
            "SELECT type FROM facts WHERE fact_id = ?", ("f1",)
        ).fetchone()
        assert row["type"] == "semantic"
    finally:
        conn.close()


def test_v1_database_upgrades_to_v2_with_type_default(tmp_path):
    """A database stuck at user_version=1 upgrades to v2 and retro-fits rows.

    Simulates an existing deployment by scripting the v1 schema directly (no
    type column), inserting a row, then reopening through open_db so the
    pending 002 migration adds the column with the default value.
    """
    import sqlite3

    path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE facts (
            fact_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            session_id TEXT NOT NULL, subject TEXT NOT NULL,
            predicate TEXT NOT NULL, object TEXT NOT NULL, qualifiers TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            importance REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            source_type TEXT NOT NULL DEFAULT 'user_explicit',
            status TEXT NOT NULL DEFAULT 'active', superseded_by TEXT,
            observed_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
            trace_id TEXT, version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE fact_candidates (
            candidate_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            session_id TEXT NOT NULL, turn_id INTEGER NOT NULL,
            raw_text TEXT, subject TEXT, predicate TEXT, object TEXT,
            qualifiers TEXT, confidence REAL DEFAULT 0.5,
            importance REAL DEFAULT 0.5, privacy TEXT DEFAULT 'private',
            status TEXT NOT NULL DEFAULT 'pending',
            idempotency_key TEXT UNIQUE, created_at INTEGER NOT NULL
        );
        CREATE TABLE user_profile (
            user_id TEXT NOT NULL, section TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'system_inferred',
            confidence REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, section, key)
        );
        PRAGMA user_version = 1;
        """
    )
    raw.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, observed_at, created_at) VALUES ('f1','u1','s1','用户',"
        "'偏好','黑咖啡',1000,1000)"
    )
    # A genuine v1 database already carries user_profile (migration 001 creates
    # it) — the later `pinned` migration alters exactly this table.
    raw.execute(
        "INSERT INTO user_profile(user_id, section, key, value, updated_at) "
        "VALUES ('u1','职业','value','工程师',1000)"
    )
    raw.commit()
    raw.close()

    conn = open_db(MemConfig(db_path=path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        row = conn.execute(
            "SELECT type FROM facts WHERE fact_id = ?", ("f1",)
        ).fetchone()
        assert row is not None
        assert row["type"] == "semantic"
        prof = conn.execute("SELECT pinned FROM user_profile").fetchone()
        assert prof["pinned"] == 0
    finally:
        conn.close()


def test_v2_database_upgrades_to_v3_with_null_content(tmp_path):
    """A database at user_version=2 upgrades through v3 (NULL content column)
    and v4 (profile `pinned`) in one open."""
    import sqlite3

    path = str(tmp_path / "legacy_v2.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE facts (
            fact_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
            session_id TEXT NOT NULL, subject TEXT NOT NULL,
            predicate TEXT NOT NULL, object TEXT NOT NULL, qualifiers TEXT,
            confidence REAL NOT NULL DEFAULT 0.5,
            importance REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            source_type TEXT NOT NULL DEFAULT 'user_explicit',
            status TEXT NOT NULL DEFAULT 'active', superseded_by TEXT,
            observed_at INTEGER NOT NULL, created_at INTEGER NOT NULL,
            trace_id TEXT, version INTEGER NOT NULL DEFAULT 1,
            type TEXT NOT NULL DEFAULT 'semantic'
        );
        CREATE TABLE fact_candidates (
            candidate_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            turn_id INTEGER NOT NULL,
            raw_text TEXT,
            subject TEXT,
            predicate TEXT,
            object TEXT,
            qualifiers TEXT,
            confidence REAL DEFAULT 0.5,
            importance REAL DEFAULT 0.5,
            privacy TEXT DEFAULT 'private',
            status TEXT NOT NULL DEFAULT 'pending',
            idempotency_key TEXT UNIQUE,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE user_profile (
            user_id TEXT NOT NULL, section TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'system_inferred',
            confidence REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, section, key)
        );
        PRAGMA user_version = 2;
        """
    )
    raw.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, observed_at, created_at) VALUES ('f1','u1','s1','用户',"
        "'偏好','黑咖啡',1000,1000)"
    )
    raw.execute(
        "INSERT INTO user_profile(user_id, section, key, value, updated_at) "
        "VALUES ('u1','职业','value','工程师',1000)"
    )
    raw.commit()
    raw.close()

    conn = open_db(MemConfig(db_path=path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        row = conn.execute(
            "SELECT type, content FROM facts WHERE fact_id = ?", ("f1",)
        ).fetchone()
        # pre-existing rows get type default and NULL content
        assert row["type"] == "semantic"
        assert row["content"] is None
        # ...and pre-existing profile rows stay live but unpinned.
        assert conn.execute("SELECT pinned FROM user_profile").fetchone()["pinned"] == 0
        # column accepts a structured body
        conn.execute(
            "UPDATE facts SET content = ? WHERE fact_id = ?",
            ('{"steps":["a","b"]}', "f1"),
        )
        conn.commit()
        assert conn.execute(
            "SELECT content FROM facts WHERE fact_id = ?", ("f1",)
        ).fetchone()["content"] == '{"steps":["a","b"]}'
    finally:
        conn.close()


def test_v3_database_upgrades_to_v4_unpinned(tmp_path):
    """A database at user_version=3 gains `pinned`, defaulted to "not pinned".

    Migration 004 must retro-fit rows that predate the pin: an existing
    deployment's profile is live and editable, never silently frozen.
    """
    import sqlite3

    path = str(tmp_path / "legacy_v3.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE user_profile (
            user_id TEXT NOT NULL, section TEXT NOT NULL, key TEXT NOT NULL,
            value TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'system_inferred',
            confidence REAL NOT NULL DEFAULT 0.5,
            privacy TEXT NOT NULL DEFAULT 'private',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, section, key)
        );
        INSERT INTO user_profile(user_id, section, key, value, updated_at)
            VALUES ('u1','职业','value','工程师',1000);
        PRAGMA user_version = 3;
        """
    )
    raw.commit()
    raw.close()

    conn = open_db(MemConfig(db_path=path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        row = conn.execute("SELECT value, pinned FROM user_profile").fetchone()
        assert row["value"] == "工程师"
        assert row["pinned"] == 0
    finally:
        conn.close()

