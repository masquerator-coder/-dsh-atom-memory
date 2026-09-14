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

Ordering blends **importance and recency** into one score (see
:func:`_blend`): a fact's ``importance`` only counts when the extractor actually
supplied a signal; the neutral default (:data:`NEUTRAL_SCORE`) means "unknown"
and falls back to the type's default rank
(:func:`~atom_memory.models.default_importance`). Without that fallback every
fact ties at 0.5 and the order degenerates to plain recency.

Recency is a real dimension rather than a tie-break, because a memory view is
read at whatever budget the user configured: when the budget is tight, whatever
the ordering puts last is what gets dropped. Ranking by importance alone would
therefore always sacrifice the newest material — a fact recorded minutes ago
would lose to durable knowledge from months back — which is exactly the wrong
trade for "what is going on right now". The blend keeps importance primary
while letting a sufficiently fresh fact outrank a stale one.

The budget is spent **globally best-first** (:func:`_select`), not
section-by-section: trimming a whole low-priority section before touching any
line of a high-priority one would discard a top-scoring fresh fact merely
because it lives in the section that sorts last.
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

# -- ranking ------------------------------------------------------------------
#
# One score per fact decides both the render order and (through it) what a tight
# token budget keeps. The two weights are deliberately explicit module constants
# rather than magic numbers inline: they are the one knob that trades "durable
# knowledge stays" against "what just happened gets in".
#
# Importance is primary (0.7) because the type defaults already encode what stays
# valuable longest; recency (0.3) is strong enough to promote a fresh fact past
# materially staler material without letting recency alone decide the view.
_IMPORTANCE_WEIGHT = 0.7
_RECENCY_WEIGHT = 0.3

# Age at which a fact's recency credit halves. Age is measured *relative to the
# newest fact in the set* rather than against the wall clock, so the ranking is
# deterministic (no clock dependency, no test flakiness) and still means what it
# should: "the newest thing I know" always gets full recency credit, and
# everything else is discounted by how much older it is than that.
_RECENCY_HALF_LIFE_SECONDS = 14 * 24 * 60 * 60

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


def _recency_score(created_at: int, newest_created_at: int) -> float:
    """Return the 0..1 recency credit of a fact, newest-first.

    Args:
        created_at: The fact's creation timestamp.
        newest_created_at: The newest timestamp in the same fact set.

    Returns:
        ``1.0`` for the newest fact, halving per
        :data:`_RECENCY_HALF_LIFE_SECONDS` of age relative to it.
    """
    age = max(0, int(newest_created_at) - int(created_at))
    return 0.5 ** (age / _RECENCY_HALF_LIFE_SECONDS)


def _blend(rank: float, recency: float) -> float:
    """Combine importance and recency into the single ranking score.

    Args:
        rank: The fact's importance signal (stated, else its type default).
        recency: The fact's recency credit from :func:`_recency_score`.

    Returns:
        The blended 0..1 score used for ordering and budget allocation.
    """
    return _IMPORTANCE_WEIGHT * rank + _RECENCY_WEIGHT * recency


def _sort_key(fact: dict) -> tuple:
    """Rank facts by blended score, then recency, then a stable id tiebreak."""
    return (-fact["score"], -int(fact["created_at"]), str(fact["fact_id"]))


def _collect(conn: sqlite3.Connection, user_id: str) -> dict:
    """Bucket a user's active facts by rendered section, score-sorted.

    Sections are ordered by the blended score of their **best** fact, not by a
    fixed type list: a fresh, high-importance fact must be able to outrank a
    whole section of staler ones, otherwise "top of the view" would mean
    "luckiest type" rather than "most worth reading now". ``_SECTION_ORDER`` only
    breaks ties between sections whose best facts score equally.

    Returns:
        An ordered mapping ``section -> [fact, ...]``. Empty when the user has
        no active facts.
    """
    facts = _load_active_facts(conn, user_id)
    if not facts:
        return {}

    newest = max(int(fact["created_at"]) for fact in facts)
    for fact in facts:
        fact["score"] = _blend(fact["rank"], _recency_score(fact["created_at"], newest))

    grouped: dict = {}
    for fact in facts:
        grouped.setdefault(_section_of(fact), []).append(fact)
    for items in grouped.values():
        items.sort(key=_sort_key)

    ranked = sorted(
        grouped.items(),
        key=lambda item: (
            -max(fact["score"] for fact in item[1]),
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
    sections: dict = {}  # section title -> [(line, score), ...] in render order
    for section, facts in buckets.items():
        if not facts:
            continue
        rendered = _render_section_lines(section, facts)
        if not rendered:
            continue
        sections[_SECTION_TITLES[section]] = rendered

    total = sum(len(lines) for lines in sections.values())
    rendered = _compose(sections, _select(sections, max_tokens))
    if estimate_tokens(rendered) <= max_tokens:
        return rendered
    # The budget is smaller than even the empty digest + footer costs. Nothing
    # per-type can be said within it, so emit the shortest honest thing rather
    # than silently overshooting the caller's cap.
    return f"> {total} 条记忆已省略（预算不足，请用 memory_recall 检索）"


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


def _render_section_lines(section: str, facts: List[dict]) -> List[Tuple[str, float]]:
    """Render one section's facts to scored bullet lines, highest score first.

    Each line carries the blended score of its best contributing fact, because
    that score is what decides whether the line survives a tight budget. Folded
    lines (see :func:`_fold_preferences` / :func:`_attribute_lines`) therefore
    inherit their strongest member's score: a line is worth as much as the best
    thing it stands for.
    """
    if section == "preference":
        return [(f"- {line}", score) for line, score in _fold_preferences(facts)]
    if section == "attribute":
        return [(f"- {line}", score) for line, score in _attribute_lines(facts)]
    return [(_render_fact_line(section, fact), fact["score"]) for fact in facts]


def _fold_preferences(facts: List[dict]) -> List[Tuple[str, float]]:
    """Fold multi-valued preferences into one ``- X（喜欢）`` line per polarity."""
    liked: List[str] = []
    disliked: List[str] = []
    liked_score = 0.0
    disliked_score = 0.0
    for fact in facts:
        value = str(fact["object"])
        if value in liked or value in disliked:
            continue
        if _negated(fact.get("qualifiers")):
            disliked.append(value)
            disliked_score = max(disliked_score, fact["score"])
        else:
            liked.append(value)
            liked_score = max(liked_score, fact["score"])

    lines: List[Tuple[str, float]] = []
    if liked:
        lines.append(("、".join(liked), liked_score))
    if disliked:
        lines.append(("、".join(disliked) + "（不喜欢）", disliked_score))
    return lines


def _attribute_lines(facts: List[dict]) -> List[Tuple[str, float]]:
    """Render single-valued attributes as ``predicate: value`` one-liners.

    Several values for the same predicate are merged into one line in score
    order, so a mistyped or re-stated attribute does not burn a whole
    token budget line on its own.
    """
    merged: dict = {}
    scores: dict = {}
    for fact in facts:
        predicate = fact["predicate"]
        merged.setdefault(predicate, []).append(str(fact["object"]))
        scores[predicate] = max(scores.get(predicate, 0.0), fact["score"])

    lines: List[Tuple[str, float]] = []
    for predicate, values in merged.items():
        unique: List[str] = []
        for value in values:
            if value not in unique:
                unique.append(value)
        lines.append((f"{predicate}: {'、'.join(unique)}", scores[predicate]))
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


def _select(sections: dict, max_tokens: int) -> dict:
    """Choose the lines a token budget can hold, globally best-scoring first.

    Nothing is dropped while the render fits — the dense preference/attribute
    folds are cheap and the whole point of this view is a complete picture.

    On overflow the artifact is shrunk one line at a time, always giving up the
    **globally lowest-scoring** line still present, until it fits. Two properties
    follow, and both matter:

    * The surviving set is "the most important and most recent memory" at *any*
      budget. The previous implementation instead emptied one section after
      another from the tail, so a top-scoring fact recorded minutes ago could be
      thrown away purely because it lived in the section that sorts last.
    * The budget is measured on the **assembled artifact** — the body plus the
      footer that this very selection produces — never on an estimate of its
      parts. ``estimate_tokens`` counts non-CJK characters in blocks of five per
      call, so a joined text costs *more* than the sum of its lines' costs:
      budgeting the body against a pre-subtracted footer reserve silently
      overshoots and collapses the whole render to the "omitted" notice.

    A section that loses *every* line loses its label too (labels are part of the
    artifact being measured), so no bare ``流程`` stub is rendered.

    Args:
        sections: ``section title -> [(line, score), ...]`` in render order.
        max_tokens: The budget for the complete rendered artifact.

    Returns:
        ``section title -> surviving line indices`` (ascending). Sections that
        kept nothing are absent; :func:`_compose` derives everything else
        (footer counts, hidden sections) from this one mapping, so there is a
        single source of truth for what was kept.
    """
    titles = list(sections)
    kept: dict = {title: list(range(len(lines))) for title, lines in sections.items()}

    # Lowest score first: the order in which material is given up. Ties resolve
    # against render order so the outcome is deterministic.
    give_up = [
        (score, order_index, line_index)
        for order_index, lines in enumerate(sections.values())
        for line_index, (_line, score) in enumerate(lines)
    ]
    give_up.sort(key=lambda item: (item[0], -item[1], -item[2]))

    for _score, order_index, line_index in give_up:
        if estimate_tokens(_compose(sections, kept)) <= max_tokens:
            break
        title = titles[order_index]
        if line_index in kept[title]:
            kept[title] = [index for index in kept[title] if index != line_index]

    return {title: indices for title, indices in kept.items() if indices}


def _kept_indices(kept: dict, title: str) -> List[int]:
    """Return a section's kept line indices (empty list when it kept none)."""
    return kept.get(title) or []


def _render_body(sections: dict, kept: dict) -> str:
    """Assemble the surviving lines, grouped under their section labels.

    Labels are emitted only for sections that kept at least one line, and lines
    keep their original (score-descending) order inside a section: the selection
    chooses *what* survives, never the layout.
    """
    parts: List[str] = []
    for title, lines in sections.items():
        indices = _kept_indices(kept, title)
        if not indices:
            continue
        parts.append(title)
        parts.extend(lines[index][0] for index in indices)
    return "\n".join(parts)


def _compose(sections: dict, kept: dict) -> str:
    """Render the complete artifact (body + footer) for a candidate selection.

    The footer is derived from the same selection, so what the fit check measures
    is exactly what the caller receives — including the "what was left out" line,
    whose length depends on how much was dropped.
    """
    counts = {title: len(indices) for title, indices in kept.items() if indices}
    hidden = [title for title in sections if not kept.get(title)]
    omitted = sum(len(lines) for lines in sections.values()) - sum(counts.values())
    body = _render_body(sections, kept)
    footer = _render_footer(omitted, hidden, counts)
    return body + "\n\n" + footer if body else footer


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
