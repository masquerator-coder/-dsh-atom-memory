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
SCHEMA_VERSION = 6


def now_ms() -> int:
    """Return the current wall-clock time in milliseconds.

    Used as the canonical timestamp unit for all ``*_at`` columns.
    """
    return int(time.time() * 1000)


# -- time decay ---------------------------------------------------------------
#
# Every time-based credit in the library is one shape: an exponential decay with
# a half-life. It lives here, next to :func:`now_ms`, because this module is
# already the single authority on what a timestamp means, and because both
# consumers (the retriever's ranking and ``summary``'s budget allocation) must
# agree on the shape even though they tune different half-lives. The alternative
# — a per-query min-max normalisation of ages — is what this replaces: it
# rescales the candidate set so the best fact scores 1.0 and the worst 0.0
# *whatever the actual spread is*, which means two facts written milliseconds
# apart inside one session are treated as maximally different in age, and one
# ancient outlier makes everything else maximally fresh.

MS_PER_DAY = 86_400_000.0


def recency_credit(
    age: float,
    half_life: float,
    reference_offset: float = 0.0,
) -> float:
    """Return a 0..1 exponential decay credit for an age.

    **Units are whatever the caller passes, as long as they match.** ``age``,
    ``half_life`` and ``reference_offset`` must all be in the same unit (this
    library's timestamps are milliseconds; callers that already have seconds work
    in seconds). The function deliberately does no conversion of its own: a
    silent ms/s mismatch is invisible in a formula and produces plausible-looking
    numbers, so the unit is the caller's single source of truth.

    Args:
        age: How old the item is, measured against the same clock as every other
            item in the set.
        half_life: Age at which the credit halves. Must be positive.
        reference_offset: Age that should score ``1.0``. Ages at or below it
            clamp to ``1.0``, so it is a floor on the credit rather than
            something a slightly-newer item can exceed.

    Returns:
        ``1.0`` at ``reference_offset``, halving per ``half_life`` of additional
        age, never negative.
    """
    if half_life <= 0:
        raise ValueError("half_life must be positive")
    decayed = max(0.0, float(age) - float(reference_offset))
    return 0.5 ** (decayed / half_life)


def age_offset(age: float, min_age: float, window: float) -> float:
    """Map an age onto the 0..window range that earns recency credit.

    Recency is *relative*: the newest item in a set defines "current", because a
    wall-clock age cannot be compared meaningfully against other items in the
    same result set (see :mod:`~atom_memory.retriever` for why). So ages are
    shifted so that the newest item becomes ``0``, and the shift is capped at
    ``window``.

    The cap is what prevents the opposite failure: if the newest item is itself
    very old, an uncapped shift would push every item past the clamp and hand
    them all identical full credit, silently switching the recency term off.

    All three arguments must be in the same unit as the ``age``/``half_life``
    passed to :func:`recency_credit`.

    Args:
        age: The item's age.
        min_age: The smallest age in the set (the newest item), >= 0.
        window: How far back the shift may reach, > 0.

    Returns:
        ``max(0, age - min_age)`` capped at ``window``.
    """
    if window <= 0:
        raise ValueError("window must be positive")
    return min(max(0.0, float(age) - max(0.0, float(min_age))), float(window))


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

