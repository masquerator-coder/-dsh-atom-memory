"""Validation chain for atomic facts.

The validator applies a fixed sequence of checks to a candidate before it is
allowed into the ``facts`` (and derived FTS / vector) tables:

    empty -> confidence -> idempotency -> conflict -> privacy

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

    # 2. Confidence
    conf = _check_confidence(candidate)
    if not conf.ok:
        return conf

    # 3. Idempotency
    idem = _check_idempotency(candidate, conn)
    if not idem.ok:
        return idem

    # 4. Conflict
    conflict = _check_conflict(candidate, conn)
    if not conflict.ok:
        return conflict

    # 5. Privacy
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
        return ValidationResult.fail(
            "idempotent",
            f"idempotency_key already used by candidate {row['candidate_id']}",
            candidate.candidate_id,
            suppressed=row["candidate_id"],
        )
    return ValidationResult.pass_(candidate.candidate_id)


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
    episodic = cand_type == "episodic" or candidate.predicate == "事件"
    # Knowledge items stay independent under a shared predicate: a second SOP or
    # lesson is a new item, not a contradiction of the first.
    multi_valued = (
        candidate.predicate in MULTI_VALUED_PREDICATES
        or episodic
        or cand_type in ALL_KNOWLEDGE
    )

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
