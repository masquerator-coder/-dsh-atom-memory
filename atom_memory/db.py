"""SQLite connection management for dsh-atom-memory.

This module is responsible for opening a WAL-mode SQLite connection, loading
the ``sqlite-vec`` extension, applying PRAGMAs and executing pending schema
migrations from the bundled ``migrations/`` package.
"""

from __future__ import annotations

import importlib.resources
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

import sqlite_vec

from .config import MemConfig

logger = logging.getLogger(__name__)

# The highest schema version the bundled migrations know about.
SCHEMA_VERSION = 4


def now_ms() -> int:
    """Return the current wall-clock time in milliseconds.

    Used as the canonical timestamp unit for all ``*_at`` columns.
    """
    return int(time.time() * 1000)


def _read_migration(name: str) -> str:
    """Read a migration script from the bundled migrations package.

    Args:
        name: Base filename of the migration (e.g. ``"001_init.sql"``).

    Returns:
        The raw SQL text of the migration.
    """
    text = importlib.resources.files("atom_memory.migrations").joinpath(name).read_text(
        encoding="utf-8"
    )
    return text


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Apply any pending migrations, gated by ``PRAGMA user_version``.

    Each migration raises ``user_version`` to its own number once applied.
    The reference schema version matches the highest migration number; running
    ahead of it simply does nothing.

    Args:
        conn: An open SQLite connection.
    """
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    target = max(SCHEMA_VERSION, current)
    for version in range(current + 1, target + 1):
        script = _read_migration(f"{version:03d}_init.sql")
        with conn:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {version}")
        logger.info("Applied migration %03d (user_version -> %d)", version, version)


def open_db(config: MemConfig) -> sqlite3.Connection:
    """Open (or create) the SQLite database and prepare it for use.

    Steps performed:
        - expand ``~`` in the configured path and create parent directories;
        - open the connection;
        - load the ``sqlite-vec`` extension;
        - apply WAL and foreign-key PRAGMAs;
        - run any pending migrations.

    Args:
        config: Library configuration carrying the database path.

    Returns:
        A configured :class:`sqlite3.Connection` ready for queries.

    Raises:
        sqlite3.Error: If the database cannot be opened or the vec extension
            cannot be loaded.
    """
    db_path = config.resolved_db_path()
    parent = Path(db_path).parent
    if str(parent) and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # sqlite-vec's load() merely calls conn.load_extension(); Python 3.11+
    # ships SQLite with extension loading disabled by default, so it must be
    # enabled on this connection before the vec0 virtual table can be loaded.
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)

    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")

    _apply_migrations(conn)

    logger.info("Opened database at %s (user_version=%d)", db_path, SCHEMA_VERSION)
    return conn


def connect_for_tests(config: Optional[MemConfig] = None) -> sqlite3.Connection:
    """Open a throwaway in-memory database useful for tests.

    Args:
        config: Optional configuration; defaults to a temporary in-memory DB.

    Returns:
        A prepared :class:`sqlite3.Connection` backed by ``:memory:``.
    """
    cfg = config or MemConfig(db_path=":memory:")
    cfg.db_path = ":memory:"
    return open_db(cfg)

