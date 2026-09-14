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
    pinned: Optional[bool] = None,
) -> bool:
    """Insert or update a profile row under source-priority rules.

    If the key already exists with a *more* authoritative source, the incoming
    (weaker) value is dropped and ``False`` is returned. Otherwise the row is
    written (or upgraded) and ``True`` is returned.

    A **pinned** row (the user's 固定 flag) is frozen against this path
    entirely: the projection from facts is derived, and a derived write must
    never update or replace something the user declared fixed. Passing ``pinned``
    — ``True`` or ``False`` — is the pin owner's write (the settings panel
    deciding the row's state, including releasing it), and is the only way
    through; every automatic caller leaves ``pinned`` at ``None``.

    Args:
        conn: The SQLite connection.
        user_id: Owner of the profile row.
        section: Profile section (e.g. a predicate like ``职业``).
        key: Key within the section.
        value: The stored value.
        source: Credibility source tag.
        confidence: 0..1 confidence.
        privacy: Privacy tag.
        pinned: New pin state; ``None`` keeps the row's current state (and means
            "not pinned" for a brand-new row).

    Returns:
        ``True`` if the write was applied, ``False`` if a stronger source
        already held the key or the row is pinned and the caller did not state a
        pin state.
    """
    existing = conn.execute(
        "SELECT source, pinned FROM user_profile "
        "WHERE user_id = ? AND section = ? AND key = ?",
        (user_id, section, key),
    ).fetchone()

    if existing is not None and bool(existing["pinned"]) and pinned is None:
        return False

    if existing is not None and source_priority(source) < source_priority(
        existing["source"]
    ):
        return False

    if pinned is None:
        pinned_flag = int(bool(existing["pinned"])) if existing is not None else 0
    else:
        pinned_flag = int(bool(pinned))

    conn.execute(
        "INSERT INTO user_profile(user_id, section, key, value, source, "
        "confidence, privacy, pinned, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, section, key) DO UPDATE SET "
        "value = excluded.value, source = excluded.source, "
        "confidence = excluded.confidence, privacy = excluded.privacy, "
        "pinned = excluded.pinned, updated_at = excluded.updated_at",
        (user_id, section, key, value, source, confidence, privacy, pinned_flag, now_ms()),
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
    more-authoritative facts can override but never get downgraded. Rows the
    user pinned are skipped outright — this projection is derived, and a fixed
    row is by definition not the projection's to change.

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
        "SELECT section, key, value, source, pinned FROM user_profile "
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

    return "\n".join(lines)


def _render_row(r) -> str:
    """Render one profile row, including its key when it is not 'value'.

    Pinned rows carry an explicit 固定 marker: a reader (the model, through the
    ``memory_user_md`` tool) should know which attributes the user froze against
    automatic memory, since those are the ones whose stability is deliberate.
    """
    if r["key"] == "value":
        body = f"**{r['section']}**: {r['value']}"
    else:
        body = f"**{r['section']}**: {r['key']} = {r['value']}"
    pinned = " · 固定" if r["pinned"] else ""
    return f"- {body}  *(来源 {r['source']}{pinned})*"


def _negation(qualifiers: Optional[str]) -> bool:
    """Return whether a qualifiers JSON string carries a negation marker."""
    try:
        q = json.loads(qualifiers) if qualifiers else {}
    except (ValueError, TypeError):
        q = {}
    return bool(q.get("negation"))
