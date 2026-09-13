"""Generate a user's ``memory.md`` summary from active atomic facts.

``memory.md`` is a *derived view* over the authoritative facts table: it lists
active facts (with their stable ``fact_id``), newest-and-most-important first,
trimmed to a token budget.
"""

from __future__ import annotations

import sqlite3

from .retriever import estimate_tokens


def generate_memory_md(
    conn: sqlite3.Connection,
    user_id: str,
    max_tokens: int = 1500,
) -> str:
    """Build the ``memory.md`` text for a user.

    Args:
        conn: The SQLite connection.
        user_id: The user whose memory is rendered (hard isolation scope).
        max_tokens: Upper bound on the estimated token count of the body
            (headings are always kept).

    Returns:
        The rendered markdown string. Contains ``fact_id`` for every listed
        fact and an empty list section when the user has no active facts.
    """
    facts = _load_active_facts(conn, user_id)

    if not facts:
        return (
            f"# 记忆 (Memory) — {user_id}\n\n"
            "_暂无持久化的原子记忆。_ (No active atomic facts yet.)\n"
        )

    # Newest first, then by importance descending.
    facts.sort(key=lambda f: (-int(f["created_at"]), -float(f["importance"])))

    lines = [f"# 记忆 (Memory) — {user_id}", ""]
    budget = max_tokens
    kept = 0
    budget_exhausted = False

    for fact in facts:
        line = _format_fact(fact)
        if estimate_tokens(line) > budget and kept > 0:
            budget_exhausted = True
            break
        lines.append(f"- {line}")
        budget -= estimate_tokens(line)
        kept += 1

    lines.append("")
    lines.append(f"> {kept} 条事实 (facts) · 含 fact_id 作为唯一引用")
    if budget_exhausted:
        lines.append(f"> ⚠ 超出 token 预算，已裁剪（限制 {max_tokens}）")

    return "\n".join(lines)


def _load_active_facts(conn: sqlite3.Connection, user_id: str) -> list:
    """Load active facts for a user."""
    rows = conn.execute(
        "SELECT fact_id, subject, predicate, object, confidence, importance, "
        "type, content, created_at FROM facts WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def _format_fact(fact: dict) -> str:
    """Render a single fact as a markdown bullet.

    The full ``fact_id`` is included as the unique, stable reference. Facts
    that carry structured knowledge content (SOP, decision rule, few-shot,
    lesson) render the body on a folded sub-line so the full text stays
    addressable without bloating the bullet.
    """
    head = (
        f"[{fact['fact_id']}] **{fact['subject']}** — {fact['predicate']}: "
        f"{fact['object']}  *(置信 {fact['confidence']:.2f} · "
        f"重要 {fact['importance']:.2f})*"
    )
    content = fact.get("content")
    if content:
        snippet = content if len(content) <= 120 else content[:120] + "…"
        return f"{head}\n    > **知识内容** {snippet}"
    return head
