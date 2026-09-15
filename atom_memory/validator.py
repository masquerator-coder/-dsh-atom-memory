"""Validation chain for atomic facts.

The validator applies a fixed sequence of checks to a candidate before it is
allowed into the ``facts`` (and derived FTS / vector) tables:

    empty -> degenerate -> confidence -> idempotency -> conflict -> privacy

Checks that need to look at existing data ("idempotency" and "conflict")
receive the live connection so they can query stored facts.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from .models import ALL_KNOWLEDGE, FactCandidate, ValidationResult

# Allowed confidence range (inclusive).
_MIN_CONFIDENCE = 0.0
_MAX_CONFIDENCE = 1.0

# Privacy values considered acceptable; anything outside this set is rejected
# unless it matches the configured privacy filter.
_DEFAULT_PRIVACY = "private"

# Predicates that naturally hold several values at once (likes, interests,
# habits...). A different object under these predicates is an independent
# claim, not a contradiction. Everything not listed is treated as a
# single-valued attribute whose object may only have one active value.
MULTI_VALUED_PREDICATES = {
    "偏好", "兴趣", "爱好", "喜欢", "不喜欢", "习惯", "擅长",
}

# ---- degenerate-fact filtering -------------------------------------------------
#
# When a descriptive sentence *about the system itself* reaches the extractor it
# can yield a fact whose object merely echoes its own predicate
# ("被谁调用 -> 被调用的对象", "起到的作用 -> 起到的作用"). Such facts carry no
# information, yet they are persisted and then pollute the rendered summary and
# ``memory.md``. They are rejected as ``degenerate``.

# Question words removed from a predicate to obtain its "core".
_INTERROGATIVES = (
    "什么时候", "什么样", "为什么", "怎么样",
    "什么", "哪儿", "哪里", "何时", "如何", "怎么", "多少", "是否",
    "谁", "啥", "哪", "几",
)

# Generic head nouns. An object that *both* reuses the predicate core and ends
# with one of these is a placeholder, not a real value.
_GENERIC_HEADS = (
    "对象", "作用", "东西", "内容", "信息", "事情", "情况", "方面",
    "角色", "功能", "人员", "相关方", "值",
)

# Whole-object placeholders rejected outright (normalized to lower case).
_PLACEHOLDER_OBJECTS = frozenset({
    "起到的作用", "被调用的对象", "被调用者", "调用者", "对象", "作用",
    "东西", "内容", "信息", "事情", "情况", "方面", "功能", "角色",
    "未知", "待定", "不详", "其他", "其它", "无", "n/a", "na", "none", "null",
})

# Minimum share of the object length that the predicate core must cover for the
# echo heuristic to fire. Guards against false positives such as
# "角色: 项目经理的角色" (core 2 chars / object 7 chars = 0.29 -> kept).
_ECHO_MIN_RATIO = 0.5

# Punctuation/whitespace trimmed from both ends during normalization.
_TRIM_CHARS = " \t\r\n。．.！！?？；;，,、：:"


def validate(
    candidate: FactCandidate,
    conn: Optional[sqlite3.Connection] = None,
    privacy_filter: str = _DEFAULT_PRIVACY,
    forbid_qualifier_fields: bool = True,
) -> ValidationResult:
    """Run the full validation chain on a candidate.

    Args:
        candidate: The candidate to validate.
        conn: Optional connection used for idempotency and conflict lookups.
            If ``None`` those checks short-circuit to passing (write-time
            enforcement still applies via the DB constraints).
        privacy_filter: Configured privacy tag; if ``candidate.privacy`` does
            not match it, the ``privacy`` check fails.
        forbid_qualifier_fields: Reserved for future use; kept for API
            stability.

    Returns:
        A :class:`ValidationResult` describing the outcome.
    """
    # 1. Empty
    empty = _check_empty(candidate)
    if not empty.ok:
        return empty

    # 2. Degenerate (placeholder / predicate-echo objects)
    degen = _check_degenerate(candidate)
    if not degen.ok:
        return degen

    # 3. Confidence
    conf = _check_confidence(candidate)
    if not conf.ok:
        return conf

    # 4. Idempotency
    idem = _check_idempotency(candidate, conn)
    if not idem.ok:
        return idem

    # 5. Conflict
    conflict = _check_conflict(candidate, conn)
    if not conflict.ok:
        return conflict

    # 6. Privacy
    priv = _check_privacy(candidate, privacy_filter)
    if not priv.ok:
        return priv

    return ValidationResult.pass_(candidate.candidate_id)


def _check_empty(candidate: FactCandidate) -> ValidationResult:
    """Require a non-blank subject, predicate and object."""
    missing = [
        field
        for field, value in (
            ("subject", candidate.subject),
            ("predicate", candidate.predicate),
            ("object", candidate.object),
        )
        if not (value and str(value).strip())
    ]
    if missing:
        return ValidationResult.fail(
            "empty",
            f"required fields empty: {', '.join(missing)}",
            candidate.candidate_id,
        )
    return ValidationResult.pass_(candidate.candidate_id)


def _check_degenerate(candidate: FactCandidate) -> ValidationResult:
    """Reject a candidate whose object carries no information.

    Three independent rules, applied to the normalized subject / predicate /
    object:

    A. the object merely repeats the subject or the predicate
       ("起到的作用 -> 起到的作用");
    B. the object is a known placeholder phrase ("对象", "待定", ...);
    C. the object reuses the predicate core (the predicate with its question
       words removed) *and* ends with a generic head noun, while covering at
       least :data:`_ECHO_MIN_RATIO` of the object
       ("被谁调用 -> 被调用的对象").

    The length guard in C keeps informative objects such as
    "角色: 项目经理的角色" alive.
    """
    subject = _normalize(candidate.subject)
    predicate = _normalize(candidate.predicate)
    obj = _normalize(candidate.object)

    # A. Object echoes the subject or the predicate.
    if obj and obj in (subject, predicate):
        echoed = "subject" if obj == subject else "predicate"
        return ValidationResult.fail(
            "degenerate",
            f"object '{candidate.object}' only echoes the {echoed}",
            candidate.candidate_id,
        )

    # B. Object is a placeholder phrase.
    if obj in _PLACEHOLDER_OBJECTS:
        return ValidationResult.fail(
            "degenerate",
            f"object '{candidate.object}' is a placeholder with no content",
            candidate.candidate_id,
        )

    # C. Object reuses the predicate core and ends with a generic head noun.
    core = _predicate_core(predicate)
    if (
        core
        and core in obj
        and obj.endswith(_GENERIC_HEADS)
        and len(core) >= _ECHO_MIN_RATIO * len(obj)
    ):
        return ValidationResult.fail(
            "degenerate",
            f"object '{candidate.object}' restates the predicate "
            f"'{candidate.predicate}' as a placeholder",
            candidate.candidate_id,
        )

    return ValidationResult.pass_(candidate.candidate_id)


def _normalize(value: Optional[str]) -> str:
    """Lower-case, collapse whitespace and trim surrounding punctuation."""
    text = " ".join(str(value or "").split())
    text = text.strip(_TRIM_CHARS)
    # Drop a leading/trailing possessive particle ("的的作用" -> "作用").
    while len(text) > 1 and text.startswith("的"):
        text = text[1:]
    while len(text) > 1 and text.endswith("的"):
        text = text[:-1]
    return text.lower()


def _predicate_core(predicate: str) -> str:
    """Return the predicate with its question words removed.

    Longer question words are removed first so that "哪里" is not reduced to a
    stray "里" by the shorter "哪".
    """
    core = predicate
    for word in sorted(_INTERROGATIVES, key=len, reverse=True):
        core = core.replace(word, "")
    return core.strip(_TRIM_CHARS).strip()


def _check_confidence(candidate: FactCandidate) -> ValidationResult:
    """Require confidence to lie within the allowed range."""
    c = float(candidate.confidence)
    if not (_MIN_CONFIDENCE <= c <= _MAX_CONFIDENCE):
        return ValidationResult.fail(
            "confidence",
            f"confidence {c} out of range [{_MIN_CONFIDENCE}, {_MAX_CONFIDENCE}]",
            candidate.candidate_id,
        )
    return ValidationResult.pass_(candidate.candidate_id)


def _check_idempotency(
    candidate: FactCandidate, conn: Optional[sqlite3.Connection]
) -> ValidationResult:
    """Suppress a candidate whose idempotency key is already recorded.

    The UNIQUE constraint on ``fact_candidates.idempotency_key`` enforces this
    at write time; this check gives a friendly early signal. When the key is
    absent the check passes (no dedup requested).
    """
    key = candidate.idempotency_key
    if not key or conn is None:
        return ValidationResult.pass_(candidate.candidate_id)

    row = conn.execute(
        "SELECT candidate_id FROM fact_candidates WHERE idempotency_key = ?",
        (key,),
    ).fetchone()
    if row is not None:
        # ``suppressed``'s contract is "the existing *fact_id* that already
        # represents this candidate". The prior row is a *candidate*, not a
        # fact, but when that claim has since been applied to an active fact of
        # the same owner + SPO, handing back the real ``fact_id`` lets the
        # worker record the reuse-reinforcement (see worker._process_extract):
        # restating an already-stored claim is the cleanest reuse evidence the
        # library can observe, and this idempotency-key dedup fires *before* the
        # same-SPO path in ``_check_conflict``, so without this the signal would
        # never land. When no such active fact exists (the prior candidate was
        # skipped, or the fact was retracted) there is nothing to reinforce, and
        # ``suppressed`` is left unset for a pure suppress — never a candidate id
        # masquerading as a fact id.
        fact = conn.execute(
            "SELECT fact_id FROM facts WHERE user_id = ? AND subject = ? "
            "AND predicate = ? AND object = ? AND status = 'active' "
            "ORDER BY created_at ASC LIMIT 1",
            (
                candidate.user_id,
                candidate.subject,
                candidate.predicate,
                candidate.object,
            ),
        ).fetchone()
        return ValidationResult.fail(
            "idempotent",
            f"idempotency_key already used by candidate {row['candidate_id']}",
            candidate.candidate_id,
            suppressed=fact["fact_id"] if fact is not None else None,
        )
    return ValidationResult.pass_(candidate.candidate_id)


def is_multi_valued(predicate: str, memory_type: str) -> bool:
    """Whether an SPO (predicate, type) may legitimately hold many objects.

    A predicate is multi-valued when it is a preference/collection predicate
    (``MULTI_VALUED_PREDICATES``), an episodic event, or a knowledge item
    (``ALL_KNOWLEDGE``) — under any of these a *different* object is an
    independent claim, not a contradiction. Everything else is a single-valued
    attribute whose object may only have one active value.

    This is the single authority for the rule, shared by the write-path conflict
    check (:func:`_check_conflict`) and the read-path conflict report
    (:func:`atom_memory.api.AtomMem.recall`), so both stay in agreement.
    """
    if predicate in MULTI_VALUED_PREDICATES:
        return True
    if memory_type == "episodic" or predicate == "事件":
        return True
    if memory_type in ALL_KNOWLEDGE:
        return True
    return False


def _check_conflict(
    candidate: FactCandidate, conn: Optional[sqlite3.Connection]
) -> ValidationResult:
    """Detect a contradiction against an existing active fact.

    Comparisons use the (subject, predicate) pair of the candidate against
    active facts of the same user:

    - identical object *and* identical negation  -> duplicate reinforcement,
      reported as ``idempotent`` (superseded by the existing fact);
    - identical object *but* flipped negation     -> direct contradiction,
      reported as ``conflict``;
    - different object:
        * multi-valued predicate (preferences, interests...) -> an independent
          claim, **not** a conflict;
        * knowledge fact (sop / few_shot / decision_rule / lesson) -> independent
          items that legitimately share one predicate (two SOPs, two lessons...),
          **not** a conflict;
        * episodic event (type=episodic or predicate ``事件``) -> events are
          naturally many and independent, **not** a conflict;
        * single-valued predicate (attribute or one workflow name) -> the
          previous value conflicts, reported as ``conflict``.

    Inactive (e.g. ``superseded`` / ``retracted``) facts are ignored.
    """
    if conn is None or not candidate.subject or not candidate.predicate:
        return ValidationResult.pass_(candidate.candidate_id)

    cand_obj = (candidate.object or "").strip()
    cand_neg = _has_negation(candidate.qualifiers)
    cand_type = getattr(candidate, "type", "semantic") or "semantic"
    # Knowledge items stay independent under a shared predicate: a second SOP or
    # lesson is a new item, not a contradiction of the first.
    multi_valued = is_multi_valued(candidate.predicate, cand_type)

    rows = conn.execute(
        "SELECT fact_id, object, qualifiers FROM facts "
        "WHERE user_id = ? AND subject = ? AND predicate = ? "
        "AND status = 'active'",
        (candidate.user_id, candidate.subject, candidate.predicate),
    ).fetchall()
    for row in rows:
        row_obj = (row["object"] or "").strip()
        row_neg = _has_negation(row["qualifiers"])

        if row_obj == cand_obj:
            # Same underlying claim.
            if row_neg == cand_neg:
                return ValidationResult.fail(
                    "idempotent",
                    f"identical active fact already exists: {row['fact_id']}",
                    candidate.candidate_id,
                    suppressed=row["fact_id"],
                )
            return ValidationResult.fail(
                "conflict",
                f"contradicts active fact {row['fact_id']}",
                candidate.candidate_id,
                conflict_with=row["fact_id"],
            )

        # Different object text.
        if not multi_valued:
            return ValidationResult.fail(
                "conflict",
                f"conflicts with active fact {row['fact_id']}",
                candidate.candidate_id,
                conflict_with=row["fact_id"],
            )
        # Multi-valued predicate, knowledge item or episodic event with a
        # different object: independent claim, keep scanning other matching
        # facts.
        continue

    return ValidationResult.pass_(candidate.candidate_id)


def _has_negation(qualifiers: Optional[str]) -> bool:
    """Return whether a qualifiers JSON string carries a negation marker."""
    import json

    try:
        q = json.loads(qualifiers) if qualifiers else {}
    except (ValueError, TypeError):
        q = {}
    return bool(q.get("negation"))


def _check_privacy(
    candidate: FactCandidate, privacy_filter: str
) -> ValidationResult:
    """Apply / validate the privacy tag.

    A blank privacy tag is filled with the configured filter; a non-blank tag
    that disagrees with the configured filter is rejected.
    """
    tag = (candidate.privacy or "").strip() or privacy_filter
    if tag != privacy_filter:
        return ValidationResult.fail(
            "privacy",
            f"privacy '{tag}' does not match configured filter '{privacy_filter}'",
            candidate.candidate_id,
        )
    candidate.privacy = tag
    return ValidationResult.pass_(candidate.candidate_id)
