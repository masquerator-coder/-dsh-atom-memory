"""Typed data models shared across the dsh-atom-memory library.

These dataclasses describe the unit-of-work objects that flow through the
library: raw extracted fact candidates, persisted atomic facts and the result
of a validation pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Memory-type discriminators stored on facts / fact_candidates. Derived views
# (the summary view) render each type with its own format.
TYPE_SEMANTIC = "semantic"          # stable SPO knowledge: preferences, attributes
TYPE_PROCEDURAL = "procedural"      # ordered workflows / how-to experience
TYPE_EPISODIC = "episodic"          # one-off events: "at time T, X happened"
# Knowledge categories (carry an optional rich ``content`` body).
TYPE_SOP = "sop"                    # standard operating procedure (often long)
TYPE_DECISION_RULE = "decision_rule"  # if-then decision guidance
TYPE_FEW_SHOT = "few_shot"          # input -> ideal-output example pair (long)
TYPE_LESSON = "lesson"              # a distilled lesson / takeaway

# Every knowledge category carries a rich content body. Long-form ones are
# intentionally absent from the summary text (their bodies are too large to
# compress usefully), so they must be recognised and skipped — not mis-rendered
# as ordinary attributes.
ALL_KNOWLEDGE = {TYPE_SOP, TYPE_DECISION_RULE, TYPE_FEW_SHOT, TYPE_LESSON}

# Predicate used for episodic facts (events). Kept distinct so conflict
# semantics and the summary view's event detection can recognise events reliably.
PRED_EVENT = "事件"

# Value meaning "no explicit importance signal was provided". Written by every
# persistence path when the extractor (LLM or rule engine) omits the field, so
# derived views must treat it as *absent* rather than as a real 0.5 score —
# otherwise every fact ties and ordering collapses to pure recency.
NEUTRAL_SCORE = 0.5

# Fallback priority per memory type, used whenever a fact carries no explicit
# importance signal (``importance == NEUTRAL_SCORE``). The order encodes what
# stays valuable longest: durable rules and lessons outrank one-off events.
# Kept here, next to the type discriminators, so both the Python write path and
# the derived views share one authority without importing each other.
TYPE_IMPORTANCE = {
    TYPE_DECISION_RULE: 0.90,
    TYPE_LESSON: 0.85,
    TYPE_SOP: 0.80,
    TYPE_PROCEDURAL: 0.70,
    TYPE_SEMANTIC: 0.60,
    TYPE_EPISODIC: 0.50,
    TYPE_FEW_SHOT: 0.50,
}

# Precedence when several *buckets* compete for a shared token budget: durable
# knowledge first, raw chronological events last.
TYPE_ORDER = [
    TYPE_DECISION_RULE,
    TYPE_LESSON,
    TYPE_SOP,
    TYPE_PROCEDURAL,
    TYPE_SEMANTIC,
    TYPE_FEW_SHOT,
    TYPE_EPISODIC,
]


def default_importance(memory_type: Optional[str]) -> float:
    """Return the fallback importance for a memory type.

    Args:
        memory_type: A type discriminator, or ``None``/unknown.

    Returns:
        The type's fallback priority; ``NEUTRAL_SCORE`` for unknown types.
    """
    return TYPE_IMPORTANCE.get(memory_type or TYPE_SEMANTIC, NEUTRAL_SCORE)


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
