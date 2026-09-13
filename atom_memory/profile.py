"""User-profile management with source-priority upserts.

``user_profile`` is a *derived view* over atomic facts. This module owns the
priority rule: when the same (section, key) is written by two sources, the
more authoritative source wins and never gets downgraded. It also derives
profile rows from active facts and renders the profile as markdown for
``user_md``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from .db import now_ms
from .retriever import estimate_tokens
from .validator import MULTI_VALUED_PREDICATES

# Authoritative-to-weak ordering (higher = more trusted). Mirrors the
# credibility scores in retriever.SOURCE_CREDIBILITY.
SOURCE_RANK = {
    "user_explicit": 100,
    "user_confirmed": 95,
    "external_tool": 80,
    "system_inferred_high": 70,
    "indirect_inferred": 60,
    "system_inferred_low": 50,
    "model_generated": 40,
}


def source_priority(source: str) -> int:
    """Return the numeric priority of a source (higher is more trusted)."""
    return int(SOURCE_RANK.get(source, 0))


def upsert_profile(
    conn: sqlite3.Connection,
    user_id: str,
    section: str,
    key: str,
    value: str,
    source: str = "system_inferred",
    confidence: float = 0.5,
    privacy: str = "private",
) -> bool:
    """Insert or update a profile row under source-priority rules.

    If the key already exists with a *more* authoritative source, the incoming
    (weaker) value is dropped and ``False`` is returned. Otherwise the row is
    written (or upgraded) and ``True`` is returned.

    Args:
        conn: The SQLite connection.
        user_id: Owner of the profile row.
        section: Profile section (e.g. a predicate like ``职业``).
        key: Key within the section.
        value: The stored value.
        source: Credibility source tag.
        confidence: 0..1 confidence.
        privacy: Privacy tag.

    Returns:
        ``True`` if the write was applied, ``False`` if a stronger source
        already held the key.
    """
    existing = conn.execute(
        "SELECT source FROM user_profile "
        "WHERE user_id = ? AND section = ? AND key = ?",
        (user_id, section, key),
    ).fetchone()

    if existing is not None and source_priority(source) < source_priority(
        existing["source"]
    ):
        return False

    conn.execute(
        "INSERT INTO user_profile(user_id, section, key, value, source, "
        "confidence, privacy, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, section, key) DO UPDATE SET "
        "value = excluded.value, source = excluded.source, "
        "confidence = excluded.confidence, privacy = excluded.privacy, "
        "updated_at = excluded.updated_at",
        (user_id, section, key, value, source, confidence, privacy, now_ms()),
    )
    conn.commit()
    return True


def derive_profile_from_facts(conn: sqlite3.Connection, user_id: str) -> int:
    """Rebuild the user's profile from their active facts.

    Mapping:
        - single-valued attribute facts  -> (section=predicate, key='value',
          value=object)
        - multi-valued preference facts  -> (section='偏好', key=object,
          value='喜欢'/'不喜欢')

    Existing rows are updated under source priority, so more recent,
    more-authoritative facts can override but never get downgraded.

    Args:
        conn: The SQLite connection.
        user_id: Owner of the profile.

    Returns:
        Number of profile rows written or updated.
    """
    facts = conn.execute(
        "SELECT subject, predicate, object, qualifiers, confidence, "
        "privacy, source_type, type FROM facts "
        "WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()

    written = 0
    for f in facts:
        # The profile answers "who is the user", so it only reflects semantic
        # knowledge (preferences / attributes). Procedural workflows and
        # episodic events are not profile attributes and are skipped.
        if (f["type"] or "semantic") != "semantic":
            continue
        predicate = f["predicate"]
        if predicate in MULTI_VALUED_PREDICATES:
            # Preference: section 偏好, key = object, value = like/dislike.
            neg = _negation(f["qualifiers"])
            section, key, value = "偏好", f["object"], ("不喜欢" if neg else "喜欢")
        else:
            # Single-valued attribute: section = predicate, key = value.
            section, key, value = predicate, "value", f["object"]

        if upsert_profile(
            conn,
            user_id,
            section,
            key,
            value,
            source=f["source_type"],
            confidence=f["confidence"],
            privacy=f["privacy"],
        ):
            written += 1
    return written


def profile_md(
    conn: sqlite3.Connection,
    user_id: str,
    max_tokens: int = 800,
) -> str:
    """Render the user's profile as markdown for ``user_md``.

    Args:
        conn: The SQLite connection.
        user_id: Owner of the profile.
        max_tokens: Estimated token budget for the body.

    Returns:
        Markdown string, or a notice when the profile is empty.
    """
    rows = conn.execute(
        "SELECT section, key, value, source FROM user_profile "
        "WHERE user_id = ? ORDER BY section, key",
        (user_id,),
    ).fetchall()

    if not rows:
        return (
            f"# 用户画像 (User Profile) — {user_id}\n\n"
            "_暂无画像数据。_ (No profile data yet.)\n"
        )

    lines = [f"# 用户画像 (User Profile) — {user_id}", ""]
    budget = max_tokens
    for r in rows:
        line = _render_row(r)
        if estimate_tokens(line) > budget:
            break
        lines.append(line)
        budget -= estimate_tokens(line)

    lines.append("")
    lines.append(f"> 生成于 dsh-atom-memory")
    return "\n".join(lines)


def _render_row(r) -> str:
    """Render one profile row, including its key when it is not 'value'."""
    if r["key"] == "value":
        body = f"**{r['section']}**: {r['value']}"
    else:
        body = f"**{r['section']}**: {r['key']} = {r['value']}"
    return f"- {body}  *(来源 {r['source']})*"


def _negation(qualifiers: Optional[str]) -> bool:
    """Return whether a qualifiers JSON string carries a negation marker."""
    try:
        q = json.loads(qualifiers) if qualifiers else {}
    except (ValueError, TypeError):
        q = {}
    return bool(q.get("negation"))
