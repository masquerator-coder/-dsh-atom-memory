"""Generate a user's ``memory.md`` summary from active atomic facts.

``memory.md`` is a *derived view* over the authoritative facts table, rendered at
two depths from one implementation:

- **compact** (``detail=False``) — what the dsh host freezes into the session
  system prompt. Facts are grouped by memory type, semantic attributes collapse
  to ``predicate: value`` one-liners and multi-valued ones fold into a single
  line, so the whole picture fits a small token budget. No ``fact_id``: the long
  UUIDs cost more tokens than they carry information for the model, and every
  fact stays addressable through ``recall`` and the settings editor.
- **detail** (``detail=True``) — the full list, one bullet per fact with its
  ``fact_id``, used by the ``memory_memory_md`` tool and the settings modal.

Ordering is *priority first*: a fact's ``importance`` only counts when the
extractor actually supplied a signal; the neutral default (:data:`NEUTRAL_SCORE`)
means "unknown" and falls back to the type's default rank
(:func:`~atom_memory.models.default_importance`). Without that fallback every
fact ties at 0.5 and the order degenerates to plain recency.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .models import (
    NEUTRAL_SCORE,
    PRED_EVENT,
    TYPE_SEMANTIC,
    default_importance,
)
from .retriever import estimate_tokens
from .validator import MULTI_VALUED_PREDICATES

# Maximum characters of a knowledge body rendered in compact mode. The full body
# stays in the store and is reachable via ``recall`` (which returns ``content``),
# so the injected view only needs enough to recognise what the fact is.
_COMPACT_CONTENT_CHARS = 80

# Maximum characters of a knowledge body rendered in detail mode.
_DETAIL_CONTENT_CHARS = 120

# Section titles in rendering order. Sections present in the data always render
# (even when a budget only allows a heading), so the reader can see *which kinds*
# of memory exist — the failing this view used to have.
_SECTION_TITLES = {
    "decision_rule": "决策规则",
    "lesson": "教训",
    "sop": "流程（SOP）",
    "procedural": "流程",
    "preference": "偏好",
    "attribute": "属性",
    "few_shot": "示例",
    "episodic": "事件",
}

_SECTION_ORDER = [
    "decision_rule",
    "lesson",
    "sop",
    "procedural",
    "preference",
    "attribute",
    "few_shot",
    "episodic",
]

_EMPTY_NOTICE = "_暂无持久化的原子记忆。_ (No active atomic facts yet.)"


def generate_memory_md(
    conn: sqlite3.Connection,
    user_id: str,
    max_tokens: int = 1500,
    detail: bool = True,
) -> str:
    """Build the ``memory.md`` text for a user.

    Args:
        conn: The SQLite connection.
        user_id: The user whose memory is rendered (hard isolation scope).
        max_tokens: Upper bound on the estimated token count of the body.
        detail: ``True`` renders the full fact list with ``fact_id`` references;
            ``False`` renders the compact, type-grouped digest injected into the
            session system prompt.

    Returns:
        The rendered markdown string. An empty (but non-blank) notice is returned
        when the user has no active facts, in both depths.
    """
    buckets = _collect(conn, user_id)
    if not buckets:
        return _EMPTY_NOTICE if not detail else (
            f"# 记忆 (Memory) — {user_id}\n\n{_EMPTY_NOTICE}\n"
        )

    if detail:
        return _render_detail(buckets, user_id, max_tokens)
    return _render_compact(buckets, max_tokens)


# -- loading ------------------------------------------------------------------


def _load_active_facts(conn: sqlite3.Connection, user_id: str) -> List[dict]:
    """Load active facts for a user, normalising the fields the views need."""
    rows = conn.execute(
        "SELECT fact_id, subject, predicate, object, qualifiers, confidence, "
        "importance, type, content, created_at FROM facts "
        "WHERE user_id = ? AND status = 'active'",
        (user_id,),
    ).fetchall()

    facts: List[dict] = []
    for row in rows:
        fact = dict(row)
        memory_type = fact["type"] or TYPE_SEMANTIC
        fact["type"] = memory_type
        # ``importance`` is only a signal when the extractor supplied one; the
        # neutral default means "unknown" and defers to the type's rank.
        stated = float(fact["importance"] or 0.0)
        fact["rank"] = _effective_rank(memory_type, stated)
        facts.append(fact)
    return facts


def _effective_rank(memory_type: str, stated_importance: float) -> float:
    """Return the priority used for ordering: stated signal, else type default.

    Args:
        memory_type: The fact's type discriminator.
        stated_importance: The stored importance value.

    Returns:
        ``stated_importance`` when it carries a signal, otherwise the type's
        fallback rank.
    """
    if _has_signal(stated_importance):
        return stated_importance
    return default_importance(memory_type)


def _has_signal(value: float) -> bool:
    """Whether a stored 0..1 score carries an explicit signal.

    Every persistence path writes the neutral default when the extractor omits
    the field, so a stored 0.5 is indistinguishable from "unknown" and must not
    be treated as a real ranking.

    Args:
        value: The stored score.

    Returns:
        ``True`` when the value differs from the neutral default.
    """
    return abs(float(value) - NEUTRAL_SCORE) > 1e-9


def _sort_key(fact: dict) -> tuple:
    """Rank facts by priority, then recency, then a stable id tiebreak."""
    return (-fact["rank"], -int(fact["created_at"]), str(fact["fact_id"]))


def _collect(conn: sqlite3.Connection, user_id: str) -> dict:
    """Bucket a user's active facts by rendered section, priority-sorted.

    Sections are ordered by the priority of their **best** fact, not by a fixed
    type list: a stated, high-importance fact must be able to outrank a whole
    section of lower-priority ones, otherwise "top of the view" would mean
    "luckiest type" rather than "most important". ``_SECTION_ORDER`` only breaks
    ties between sections whose best facts rank equally.

    Returns:
        An ordered mapping ``section -> [fact, ...]``. Empty when the user has
        no active facts.
    """
    facts = _load_active_facts(conn, user_id)
    if not facts:
        return {}

    grouped: dict = {}
    for fact in facts:
        grouped.setdefault(_section_of(fact), []).append(fact)
    for items in grouped.values():
        items.sort(key=_sort_key)

    ranked = sorted(
        grouped.items(),
        key=lambda item: (
            -max(fact["rank"] for fact in item[1]),
            _SECTION_ORDER.index(item[0]),
        ),
    )
    return dict(ranked)


def _section_of(fact: dict) -> str:
    """Return the rendered section a fact belongs to."""
    memory_type = fact["type"]
    if memory_type == TYPE_SEMANTIC:
        predicate = fact["predicate"]
        if predicate in MULTI_VALUED_PREDICATES:
            return "preference"
        if predicate == PRED_EVENT:
            return "episodic"
        return "attribute"
    if memory_type not in _SECTION_TITLES:
        return "attribute"
    return memory_type


# -- rendering ----------------------------------------------------------------


def _render_compact(buckets: dict, max_tokens: int) -> str:
    """Render the type-grouped digest injected into the system prompt.

    Section labels are plain text rather than markdown headings: this text is
    never rendered *as* markdown — it is injected into the prompt and shown
    verbatim in a ``<pre>`` block — so ``###`` would only surface as literal
    hashes. There is likewise no document title here; the injection site
    supplies its own header and the user scope never varies, so a
    ``# 记忆 (Memory) — global`` line would be pure payload.

    The rendered artifact — footer and labels included — fits ``max_tokens``.
    The only exception is a budget too small to hold even the one-line
    "omitted" notice (~18 tokens): then that notice is returned as the shortest
    honest answer, because returning empty text would be indistinguishable from
    a failed read at the injection site.
    """
    lines: List[str] = []
    sections: dict = {}  # section title -> its rendered content lines

    for section, facts in buckets.items():
        if not facts:
            continue
        rendered = _render_section_lines(section, facts)
        if not rendered:
            continue
        title = _SECTION_TITLES[section]
        sections[title] = rendered
        lines.append(title)
        lines.extend(rendered)

    total = sum(len(body) for body in sections.values())
    trimmed = _trim(lines, list(sections.values()), max_tokens - _reserve(sections))

    kept = _kept_counts(trimmed.lines, sections, trimmed.hidden)
    footer = _render_footer(trimmed.omitted, trimmed.hidden, kept)
    body = "\n".join(trimmed.lines)
    rendered = body + "\n\n" + footer if body else footer
    if estimate_tokens(rendered) <= max_tokens:
        return rendered
    # The budget is smaller than even the empty digest + footer costs. Nothing
    # per-type can be said within it, so emit the shortest honest thing rather
    # than silently overshooting the caller's cap.
    return f"> {total} 条记忆已省略（预算不足，请用 memory_recall 检索）"


def _reserve(sections: dict) -> int:
    """Tokens to hold back so the footer always fits inside the budget.

    The footer is part of the artifact, so it is charged against the budget. It
    is reserved in its *widest* form — every section hidden, which includes the
    "已省略" clause naming them — because whether lines get dropped is exactly
    what is being decided; reserving the optimistic footer would let the dropped
    case overshoot the cap.

    Args:
        sections: ``section title -> rendered content lines``.

    Returns:
        The token estimate to subtract from the caller's budget.
    """
    return estimate_tokens(_render_footer(0, list(sections), {}))

def _render_footer(omitted: int, hidden: List[str], kept: dict) -> str:
    """Render the digest footer: what is kept, and what was left out.

    Args:
        omitted: How many content lines were dropped.
        hidden: Titles of the sections dropped in full.
        kept: The surviving ``section title -> line count`` mapping.

    Returns:
        The footer markdown.
    """
    lines = [
        f"> {sum(kept.values())} 条事实 · 类型分布："
        + " · ".join(f"{title} {count}" for title, count in kept.items())
    ]
    if omitted or hidden:
        lines.append(
            f"> 已省略 {omitted} 条低优先级记忆"
            + (f"（{'、'.join(hidden)}分组已隐藏）" if hidden else "")
            + "；需要时用 memory_recall 检索"
        )
    return "\n".join(lines)


def _kept_counts(
    lines: List[str],
    sections: dict,
    hidden: List[str],
) -> dict:
    """Count the lines that survived the trim, per section.

    A section is reported only when at least one of its lines survived, so the
    footer never advertises a group the reader cannot see.

    Args:
        lines: The surviving render, section labels interleaved.
        sections: ``section title -> rendered content lines``, in render order.
        hidden: Titles of the sections that lost every line.

    Returns:
        An ordered ``section title -> kept line count`` mapping.
    """
    present = set(lines)
    counts: dict = {}
    for title, body in sections.items():
        if title in hidden:
            continue
        alive = sum(1 for line in dict.fromkeys(body) if line in present)
        if alive:
            counts[title] = alive
    return counts


def _render_section_lines(section: str, facts: List[dict]) -> List[str]:
    """Render one section's facts to bullet lines, highest priority first.

    Preference and attribute facts are already folded into one dense line per
    value group (see :func:`_fold_preferences` / :func:`_attribute_lines`), so
    they cost far less per fact than a bullet each.
    """
    if section == "preference":
        return [f"- {line}" for line in _fold_preferences(facts)]
    if section == "attribute":
        return [f"- {line}" for line in _attribute_lines(facts)]
    return [_render_fact_line(section, fact) for fact in facts]


def _fold_preferences(facts: List[dict]) -> List[str]:
    """Fold multi-valued preferences into one ``- X（喜欢）`` line per polarity."""
    liked: List[str] = []
    disliked: List[str] = []
    for fact in facts:
        value = str(fact["object"])
        if value in liked or value in disliked:
            continue
        (disliked if _negated(fact.get("qualifiers")) else liked).append(value)

    lines: List[str] = []
    if liked:
        lines.append("、".join(liked))
    if disliked:
        lines.append("、".join(disliked) + "（不喜欢）")
    return lines


def _attribute_lines(facts: List[dict]) -> List[str]:
    """Render single-valued attributes as ``predicate: value`` one-liners.

    Several values for the same predicate are merged into one line in
    priority order, so a mistyped or re-stated attribute does not burn a whole
    token budget line on its own.
    """
    merged: dict = {}
    for fact in facts:
        merged.setdefault(fact["predicate"], []).append(str(fact["object"]))

    lines: List[str] = []
    for predicate, values in merged.items():
        unique: List[str] = []
        for value in values:
            if value not in unique:
                unique.append(value)
        lines.append(f"{predicate}: {'、'.join(unique)}")
    return lines


def _render_fact_line(section: str, fact: dict) -> str:
    """Render one fact as a compact bullet line."""
    title = _fact_title(fact, limit=_COMPACT_CONTENT_CHARS)
    if section == "episodic":
        when = _qualifier(fact.get("qualifiers"), "when")
        return f"- [{when}] {title}" if when else f"- {title}"
    return f"- {title}"


def _fact_title(fact: dict, limit: int) -> str:
    """Return the human-facing value of a fact, body-backed when available.

    Facts carrying a knowledge body render that body (truncated) rather than the
    placeholder-ish ``object`` headline, which for a long-form fact is often
    just a repeat of the predicate ("构建发布流程" → "dsh-atom-memory").
    """
    body = (fact.get("content") or "").strip()
    value = body if body else str(fact["object"]).strip()
    return _clip(value, limit) or str(fact["object"]).strip()


def _clip(text: str, limit: int) -> str:
    """Truncate a text to ``limit`` characters, appending an ellipsis."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


@dataclass(frozen=True)
class _TrimReport:
    """What the token-budget trim did to the compact render.

    Attributes:
        lines: The surviving lines, headings interleaved.
        omitted: How many content lines were dropped.
        hidden: Titles of the sections that lost every line (heading included),
            so the footer can name what the reader is not seeing.
    """

    lines: List[str]
    omitted: int
    hidden: List[str]


def _trim(
    lines: List[str],
    bodies: List[List[str]],
    max_tokens: int,
) -> _TrimReport:
    """Shrink the lowest-priority sections until the budget is met.

    Nothing is dropped while the render fits — the dense preference/attribute
    folds are cheap and the whole point of this view is a complete picture.
    On overflow, sections are shrunk from the tail inward (events first) and,
    within a section, lowest priority first. A section that loses *every* line
    loses its label too, so no bare ``流程`` stub is ever rendered, and its title
    is reported back as hidden so the footer can say what is missing.

    Args:
        lines: The flattened render (section labels interleaved).
        bodies: The rendered content of each section, in render order.
        max_tokens: The body token budget (the footer is reserved separately).

    Returns:
        The :class:`_TrimReport` describing the result.
    """
    if estimate_tokens("\n".join(lines)) <= max_tokens:
        return _TrimReport(lines=list(lines), omitted=0, hidden=[])

    omitted = 0
    hidden: List[str] = []
    pruned = list(lines)
    label_of = _label_lookup(lines, bodies)
    # Walk from the tail: each section is shrinkable until it is empty.
    for index in range(len(bodies) - 1, -1, -1):
        if estimate_tokens("\n".join(pruned)) <= max_tokens:
            break
        label = label_of[index]
        pool = list(dict.fromkeys(bodies[index]))  # de-duplicated, order kept
        while pool and estimate_tokens("\n".join(pruned)) > max_tokens:
            content = pool.pop()
            try:
                pruned.remove(content)
            except ValueError:  # pragma: no cover - defensive
                break
            omitted += 1
        if not pool:
            # The section is now empty: drop its label and record it as hidden.
            try:
                pruned.remove(label)
            except ValueError:  # pragma: no cover - defensive
                pass
            hidden.append(label)
    return _TrimReport(lines=pruned, omitted=omitted, hidden=hidden)


def _label_lookup(lines: List[str], bodies: List[List[str]]) -> List[str]:
    """Map each section body back to the label line immediately above it.

    The label line is identified by the fact that it directly precedes the
    section's first content line, so this does not depend on how labels are
    spelled.

    Args:
        lines: The flattened render (section labels interleaved).
        bodies: The rendered content of each section, in render order.

    Returns:
        The label line per section, parallel to ``bodies``.
    """
    labels: List[str] = []
    for body in bodies:
        if not body:
            labels.append("")
            continue
        index = lines.index(body[0])
        labels.append(lines[index - 1] if index > 0 else "")
    return labels


def _render_detail(buckets: dict, user_id: str, max_tokens: int) -> str:
    """Render the full fact list with ``fact_id`` references.

    The heading is kept here (unlike the compact view): this text is read by a
    human in a raw ``<pre>`` block, where the title is what orients them. The
    token budget bounds the **facts**; this depth is a drill-down list whose
    heading exists to orient a human reader and is therefore not trimmed away.
    """
    facts: List[dict] = []
    for section in buckets:
        facts.extend(buckets.get(section, []))
    facts.sort(key=_sort_key)

    lines = [f"# 记忆 (Memory) — {user_id}", ""]
    budget = max_tokens
    kept = 0
    budget_exhausted = False

    for fact in facts:
        line = f"- {_format_fact(fact)}"
        cost = estimate_tokens(line)
        if cost > budget and kept > 0:
            budget_exhausted = True
            break
        lines.append(line)
        budget -= cost
        kept += 1

    lines.append("")
    lines.append(f"> {kept} 条事实 (facts) · 含 fact_id 作为唯一引用")
    if budget_exhausted:
        lines.append(f"> ⚠ 超出 token 预算，已裁剪（限制 {max_tokens}）")
    return "\n".join(lines)


def _format_fact(fact: dict) -> str:
    """Render a single fact as a markdown bullet for the detail view.

    The full ``fact_id`` is included as the unique, stable reference. Facts that
    carry structured knowledge content (SOP, decision rule, few-shot, lesson)
    render the body on a folded sub-line so the full text stays addressable
    without bloating the bullet. The stored scores are deliberately *not*
    rendered: they are uniform in practice and read as false precision.
    """
    head = (
        f"[{fact['fact_id']}] **{fact['subject']}** — {fact['predicate']}: "
        f"{fact['object']}"
    )
    content = fact.get("content")
    if content:
        snippet = _clip(content, _DETAIL_CONTENT_CHARS)
        if snippet and snippet != str(fact["object"]).strip():
            return f"{head}\n    > **知识内容** {snippet}"
    return head


# -- qualifier helpers --------------------------------------------------------


def _qualifiers(raw: Optional[str]) -> dict:
    """Parse a qualifiers JSON string into a dict (best-effort)."""
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _qualifier(raw: Optional[str], key: str) -> str:
    """Return one qualifier value as a stripped string."""
    return str(_qualifiers(raw).get(key) or "").strip()


def _negated(raw: Optional[str]) -> bool:
    """Whether a qualifiers JSON string carries a negation marker."""
    return bool(_qualifiers(raw).get("negation"))
