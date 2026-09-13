"""Typed data models shared across the dsh-atom-memory library.

These dataclasses describe the unit-of-work objects that flow through the
library: raw extracted fact candidates, persisted atomic facts, summary
records and the result of a validation pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Memory-type discriminators stored on facts / fact_candidates. Derived views
# (summaries) render each type with its own format.
TYPE_SEMANTIC = "semantic"          # stable SPO knowledge: preferences, attributes
TYPE_PROCEDURAL = "procedural"      # ordered workflows / how-to experience
TYPE_EPISODIC = "episodic"          # one-off events: "at time T, X happened"
# Knowledge categories (carry an optional rich ``content`` body).
TYPE_SOP = "sop"                    # standard operating procedure (often long)
TYPE_DECISION_RULE = "decision_rule"  # if-then decision guidance
TYPE_FEW_SHOT = "few_shot"          # input -> ideal-output example pair (long)
TYPE_LESSON = "lesson"              # a distilled lesson / takeaway

# Knowledge categories whose content is lightweight enough to compress into the
# derived summary; long-form categories (SOP / few_shot) are excluded from the
# summary text but remain searchable and their fact_id stays tracked.
SUMMARY_LIGHT_KNOWLEDGE = {TYPE_DECISION_RULE, TYPE_LESSON}

# Every knowledge category carries a rich content body. Long-form ones are
# intentionally absent from the summary text (their bodies are too large to
# compress usefully), so they must be recognised and skipped — not mis-rendered
# as ordinary attributes.
ALL_KNOWLEDGE = {TYPE_SOP, TYPE_DECISION_RULE, TYPE_FEW_SHOT, TYPE_LESSON}
SUMMARY_EXCLUDED_KNOWLEDGE = ALL_KNOWLEDGE - SUMMARY_LIGHT_KNOWLEDGE

# Predicate used for episodic facts (events). Kept distinct so conflict
# semantics and summary bucketing can recognise events reliably.
PRED_EVENT = "事件"


@dataclass
class FactCandidate:
    """A not-yet-persisted atomic fact proposed for storage.

    Attributes:
        candidate_id: Unique identifier of this candidate (client-generated).
        user_id: Owner of the fact; all storage is scoped to this id.
        session_id: Origin session of the fact.
        turn_id: Zero-based turn within the session the fact came from.
        subject: Entity the fact is about.
        predicate: Relation between ``subject`` and ``object``.
        object: The value / complement of the fact.
        qualifiers: Optional JSON-encoded qualifier map (e.g. negation).
        confidence: 0..1 confidence in the fact's truth.
        importance: 0..1 importance / recall priority of the fact.
        privacy: Privacy tag (e.g. ``private``).
        raw_text: The original user text the fact was extracted from.
        idempotency_key: Optional stable key used to deduplicate writes.
        type: Memory type (``semantic`` / ``procedural`` / ``episodic`` /
            ``sop`` / ``decision_rule`` / ``few_shot`` / ``lesson``).
        content: Optional rich body (SOP text, few-shot example, decision-rule
            JSON...). The SPO triple acts as a searchable title; ``content``
            holds the full structured form.
    """

    candidate_id: str
    user_id: str
    session_id: str
    turn_id: int = 0
    subject: Optional[str] = None
    predicate: Optional[str] = None
    object: Optional[str] = None
    qualifiers: Optional[str] = None
    confidence: float = 0.5
    importance: float = 0.5
    privacy: str = "private"
    raw_text: Optional[str] = None
    idempotency_key: Optional[str] = None
    type: str = TYPE_SEMANTIC
    content: Optional[str] = None

    def is_complete(self) -> bool:
        """Return ``True`` when all three SPO fields are non-empty."""
        return bool(self.subject and self.predicate and self.object)


@dataclass
class AtomicFact:
    """A persisted atomic fact row (mirrors the ``facts`` table)."""

    fact_id: str
    user_id: str
    session_id: str
    subject: str
    predicate: str
    object: str
    qualifiers: Optional[str] = None
    confidence: float = 0.5
    importance: float = 0.5
    privacy: str = "private"
    source_type: str = "user_explicit"
    status: str = "active"
    superseded_by: Optional[str] = None
    observed_at: int = 0
    created_at: int = 0
    trace_id: Optional[str] = None
    version: int = 1
    type: str = TYPE_SEMANTIC
    content: Optional[str] = None


@dataclass
class Summary:
    """A persisted summary record (mirrors the ``summaries`` table)."""

    summary_id: str
    user_id: str
    scope: str
    text: str
    fact_ids: str
    theme: Optional[str] = None
    version: int = 1
    stale: int = 0
    token_count: Optional[int] = None
    updated_at: int = 0


@dataclass
class ValidationResult:
    """Outcome of validating a single fact candidate.

    Attributes:
        ok: Whether the candidate passed validation and may be persisted.
        kind: Machine-readable reason category (e.g. ``empty``,
            ``degenerate``, ``confidence``, ``idempotent``, ``conflict``,
            ``privacy``).
        reason: Human-readable explanation.
        candidate_id: The candidate this result refers to.
        conflict_with: When ``kind == "conflict"``, the fact_id the candidate
            conflicts with.
        suppressed: For ``kind == "idempotent"``, the existing fact_id that
            already represents this candidate.
    """

    ok: bool = True
    kind: str = "ok"
    reason: str = ""
    candidate_id: Optional[str] = None
    conflict_with: Optional[str] = None
    suppressed: Optional[str] = None

    @classmethod
    def pass_(cls, candidate_id: Optional[str] = None) -> "ValidationResult":
        """Return a passing validation result."""
        return cls(ok=True, kind="ok", candidate_id=candidate_id)

    @classmethod
    def fail(
        cls,
        kind: str,
        reason: str,
        candidate_id: Optional[str] = None,
        **extra: str,
    ) -> "ValidationResult":
        """Return a failing validation result with extra fields."""
        return cls(
            ok=False,
            kind=kind,
            reason=reason,
            candidate_id=candidate_id,
            **extra,
        )
