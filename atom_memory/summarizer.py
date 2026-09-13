"""Derived summary management: stale marking, debounce and rebuild.

Summaries are *derived views* over the authoritative facts table. This module
marks summaries as stale when facts change, debounces rebuilds so they do not
run on every single mutation, and rebuilds a representative aggregate from the
user's active facts.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Optional

from .db import now_ms
from .models import SUMMARY_EXCLUDED_KNOWLEDGE, SUMMARY_LIGHT_KNOWLEDGE
from .retriever import estimate_tokens

# Default summarization scope (whole-user aggregate).
SCOPE_GLOBAL = "global"


def mark_stale(conn: sqlite3.Connection, user_id: str) -> int:
    """Mark all of a user's summaries as stale.

    Called after any fact mutation (extract / replace / forget) so downstream
    views know they lag the facts.

    Args:
        conn: The SQLite connection.
        user_id: Owner of the summaries.

    Returns:
        Number of summaries marked stale.
    """
    cur = conn.execute(
        "UPDATE summaries SET stale = 1 WHERE user_id = ? AND stale = 0",
        (user_id,),
    )
    conn.commit()
    return cur.rowcount


def try_rebuild(
    conn: sqlite3.Connection,
    user_id: str,
    scope: str = SCOPE_GLOBAL,
    debounce_sec: float = 30.0,
    max_tokens: int = 1500,
) -> bool:
    """Rebuild a user's summary if the debounce interval has elapsed.

    The first build always happens; afterwards a rebuild is deferred until the
    summary is stale and ``debounce_sec`` has passed since its last update.

    Args:
        conn: The SQLite connection.
        user_id: Owner of the summary.
        scope: Summary scope key.
        debounce_sec: Minimum seconds between debounced rebuilds.
        max_tokens: Token cap for the generated summary text.

    Returns:
        ``True`` if a rebuild was performed, ``False`` if it was deferred.
    """
    existing = conn.execute(
        "SELECT summary_id, stale, updated_at FROM summaries "
        "WHERE user_id = ? AND scope = ?",
        (user_id, scope),
    ).fetchone()

    if existing is not None:
        if not existing["stale"]:
            return False  # already fresh
        if not existing["summary_id"]:
            return False
        elapsed = (now_ms() - int(existing["updated_at"])) / 1000.0
        if elapsed < debounce_sec:
            return False  # still cooling down

    rebuild_summary(conn, user_id, scope=scope, max_tokens=max_tokens)
    return True


def rebuild_summary(
    conn: sqlite3.Connection,
    user_id: str,
    scope: str = SCOPE_GLOBAL,
    max_tokens: int = 1500,
) -> Optional[str]:
    """(Re)build the user's summary from active facts.

    Only non-stale content is aggregated; ``retracted`` and ``superseded``
    facts are excluded. The summary row is upserted by (user_id, scope) with a
    monotonically increasing version.

    Args:
        conn: The SQLite connection.
        user_id: Owner of the summary.
        scope: Summary scope key.
        max_tokens: Token cap for the generated text.

    Returns:
        The summary_id written, or ``None`` if there are no active facts.
    """
    facts = conn.execute(
        "SELECT fact_id, subject, predicate, object, qualifiers, importance, "
        "type, content, created_at FROM facts WHERE user_id = ? AND status = 'active' "
        "ORDER BY importance DESC, created_at DESC",
        (user_id,),
    ).fetchall()

    if not facts:
        return None

    text, fact_ids, token_count = _aggregate(facts, max_tokens)

    # Stable, deterministic summary_id per (user_id, scope) so the upsert
    # updates the same row instead of inserting duplicates.
    summary_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"summary:{user_id}:{scope}"))

    existing = conn.execute(
        "SELECT version FROM summaries WHERE user_id = ? AND scope = ?",
        (user_id, scope),
    ).fetchone()
    version = (existing["version"] if existing else 0) + 1

    conn.execute(
        "INSERT INTO summaries(summary_id, user_id, scope, theme, text, "
        "fact_ids, version, stale, token_count, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?) "
        "ON CONFLICT(summary_id) DO UPDATE SET text = excluded.text, "
        "fact_ids = excluded.fact_ids, version = excluded.version, "
        "stale = 0, token_count = excluded.token_count, "
        "updated_at = excluded.updated_at",
        (
            summary_id,
            user_id,
            scope,
            scope,
            text,
            json.dumps(fact_ids, ensure_ascii=False),
            version,
            token_count,
            now_ms(),
        ),
    )
    conn.commit()
    return summary_id


def _aggregate(facts, max_tokens: int):
    """Compress active facts into summary text under a token budget.

    Facts are grouped by memory type so each gets a faithful, readable form:

    - semantic single-valued attributes  -> ``attr: value``
    - semantic multi-valued preferences -> ``偏好 object (喜欢|不喜欢)``
    - procedural workflows              -> ``工作流程-<name>: 1).. 2).. 3)..``
    - episodic events                   -> ``[when] event``
    - light knowledge (decision_rule, lesson) -> ``<predicate>: <object>``

    Long-form knowledge categories (``sop`` / ``few_shot``) are intentionally
    **excluded** from the summary — their bodies are too large to compress
    usefully — but their ``fact_id`` is still tracked so derived views and
    retrieval remain consistent.

    Returns ``(text, fact_ids, token_count)``.
    """
    from .validator import MULTI_VALUED_PREDICATES

    attributes: dict = {}  # predicate -> object
    prefs: dict = {}  # object -> ('喜欢'|'不喜欢')
    workflows: dict = {}  # workflow-name -> [steps]
    events: list = []  # (when, object)
    knowledge: dict = {}  # predicate -> object (light knowledge only)

    fact_ids: list = []
    for f in facts:
        fact_ids.append(f["fact_id"])
        ftype = f["type"] or "semantic"
        obj = f["object"]
        quals = _qualifiers(f["qualifiers"])

        if ftype == "procedural":
            steps = quals.get("steps") or _steps_from_object(obj)
            workflows.setdefault(f["predicate"], steps)
        elif ftype == "episodic" or f["predicate"] == "事件":
            when = (quals.get("when") or "").strip()
            events.append((when, obj))
        elif ftype in SUMMARY_LIGHT_KNOWLEDGE:
            # Lightweight knowledge fits the summary; long-form categories
            # (sop / few_shot) are skipped here.
            knowledge.setdefault(f["predicate"], obj)
        elif ftype in SUMMARY_EXCLUDED_KNOWLEDGE:
            # Long-form knowledge is intentionally not compressed into the
            # summary text (bodies too large to be useful); its fact_id is
            # still appended above so derived views remain consistent.
            continue
        elif f["predicate"] in MULTI_VALUED_PREDICATES:
            neg = _negation(f["qualifiers"])
            prefs[obj] = "不喜欢" if neg else "喜欢"
        else:
            attributes.setdefault(f["predicate"], obj)

    parts: list = []
    for attr, value in sorted(attributes.items()):
        parts.append(f"{attr}: {value}")
    for obj, like in sorted(prefs.items()):
        parts.append(f"偏好 {obj} ({like})")
    for name, steps in sorted(workflows.items()):
        parts.append(_render_workflow(name, steps))
    for when, obj in events:
        parts.append(f"[{when}] {obj}" if when else f"事件: {obj}")
    for pred, value in sorted(knowledge.items()):
        parts.append(f"{pred}: {value}")

    text = "；".join(parts) or "（无明确偏好/属性）"
    token_count = estimate_tokens(text)
    # Trim to budget by dropping the tail if needed.
    while token_count > max_tokens and len(parts) > 1:
        parts.pop()
        text = "；".join(parts) or "（无明确偏好/属性）"
        token_count = estimate_tokens(text)

    return text, fact_ids, token_count


def _render_workflow(name: str, steps) -> str:
    """Render a workflow as ``工作流程-<name>: 1)<s1> 2)<s2> ...``."""
    numbered = " ".join(f"{i + 1}){s}" for i, s in enumerate(steps if steps else [name]))
    return f"工作流程-{name}: {numbered}"


def _steps_from_object(obj: str) -> list:
    """Split a ``->``-joined procedural object back into steps."""
    if not obj:
        return []
    return [s.strip() for s in obj.split("->") if s and s.strip()]


def _qualifiers(qualifiers: Optional[str]) -> dict:
    """Parse a qualifiers JSON string into a dict (best-effort)."""
    try:
        q = json.loads(qualifiers) if qualifiers else {}
    except (ValueError, TypeError):
        q = {}
    return q if isinstance(q, dict) else {}


def _negation(qualifiers: Optional[str]) -> bool:
    """Return whether a qualifiers JSON string carries a negation marker."""
    try:
        q = json.loads(qualifiers) if qualifiers else {}
    except (ValueError, TypeError):
        q = {}
    return bool(q.get("negation"))
