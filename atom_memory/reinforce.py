"""Reuse reinforcement: saturating strengthening of frequently-used memories.

Facts are not equally valuable forever, and they are not equally valuable
because the extractor happened to score them 0.85 at write time. A fact the
user keeps re-stating (or keeps relying on) is more worth remembering than one
seen exactly once. This module turns that "reuse evidence" into an *effective*
importance, without letting repetition run away.

Design
------

A reinforcement event carries a **gain** (how strong the evidence is). Events
are folded into a single float, ``n`` ("effective reinforcement count"), which
feeds a saturating curve::

    A(n) = A_MAX * (1 - exp(-LAMBDA * n))

The curve is chosen to be simultaneously **locally linear** and **globally
bounded**:

- at ``n = 0`` the derivative equals ``A_MAX * LAMBDA``, its maximum over the
  whole domain, and for small ``n`` the curve is nearly straight
  (``1 - exp(-x) ~ x``): the first few reuses each add a comparable amount;
- the derivative decays monotonically to zero, so every further reuse adds
  strictly less than the previous one (diminishing marginal effect);
- ``A`` is bounded by ``A_MAX`` for all ``n``, so no amount of reuse can push a
  fact past the ceiling and permanently monopolise retrieval. A pure linear
  ``base + k * n`` has neither property: it is unbounded and gives the 100th
  reuse the same weight as the 1st.

The final score is ``clamp(base + A(n), 0, 1)`` where ``base`` is the
extractor's original ``importance``. Reinforcement only ever *adds* to that
baseline and is capped below 1.0, so reuse ordering can never invert the
written-down ordering of two facts that differ by more than ``A_MAX``.

Time is the second half of the design. Reuse without forgetting still
accumulates monotonically, so ``n`` is not a plain counter: it *decays*
exponentially with a half-life ``HALF_LIFE_DAYS`` and is topped up by each new
event::

    n <- n * exp(-LAMBDA_T * elapsed_days) + gain

This is what makes repeated use meaningful: the same number of events spread
over months outweighs a burst confined to one session, and a memory that stops
being used fades on its own (the "use it or lose it" half of the Ebbinghaus
picture). ``last_used_at`` is therefore *also* the better age signal for
recency ranking than ``created_at``: a long-lived but actively used fact should
not be aged out merely for being old.

State versus strength: one derivation, one place
-----------------------------------------------

The decay above is a *continuous* process but the database can only hold
discrete snapshots, so the two are kept rigorously apart:

- ``facts.reinforce_count`` + ``facts.last_used_at`` are **state**: a snapshot
  taken at the last event that passed the cooldown gate.
- ``effective_importance`` is **derived**, always, by decaying that snapshot to
  the instant being asked about (:func:`adjust` then
  :func:`effective_importance`).

Nothing may treat the snapshot as the current value. Doing so was a real defect:
a fact reinforced once and then untouched for a year kept reporting its
year-old strength forever, so "reuse decays" was true of the formula and false
of every number the system actually showed. Every reader therefore goes through
:func:`effective_importance_at` (or :func:`adjust` when it has several facts and
one reference instant), and the persisted columns are never interpreted as
strength.

There is exactly one decay function in this module and one saturating curve; the
persistence layer composes them and adds nothing of its own. That is deliberate:
the earlier shape let the write path store a partially-decayed value "as the
counter and the readers trust it as strength, which is precisely how the state and
the strength drifted apart.

What does **not** count
-----------------------

Retrieval hits are deliberately *not* a reinforcement signal. Feeding recall
back into the score creates a rich-get-richer loop in which a fact that merely
matched one query's wording gets easier to match forever. Only evidence that
the fact was genuinely reused counts, banded in :data:`KIND_GAINS`:

======================  ======  ==========================================
kind                    gain    evidence
======================  ======  ==========================================
``user_confirmed``      1.0     the user explicitly confirmed the fact
``user_restated``       0.8     the same claim was stated again, later
``applied``             0.6     the fact demonstrably shaped an answer
``retrieved_only``      0.0     mere recall: recorded, never strengthens
======================  ======  ==========================================

Anti-abuse is structural rather than ad hoc:

- **Idempotency** is enforced by a UNIQUE index on
  ``(user_id, session_id, fact_id, kind)``, so a claim restated five times in
  one session yields exactly one event.
- **Cooldown** (``COOLDOWN_SEC``) suppresses the gain when events arrive back to
  back, and the clock it measures from only advances on events that passed the
  gate. A duplicate therefore cannot preserve strength (the decay still applies)
  and cannot slide the window forward to lock a fact out of future
  reinforcement.
- **Replayability**: ``roll`` is a pure function of
  ``(n, last_used_at, gain, event_at)``, so ``fact_reinforcements`` recomputes the
  stored state exactly — which is what makes retuning ``A_MAX`` /
  ``HALF_LIFE_DAYS`` retro-applicable and suspected abuse auditable.

All functions here are pure; persistence lives in :func:`record_reinforcement`
and :func:`rebuild_fact_reinforcement`.
"""

from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from typing import Dict, Optional

from .db import now_ms

logger = logging.getLogger(__name__)

# -- tunables -----------------------------------------------------------------

# Maximum amount reinforcement may add to a fact's base importance. Deliberately
# below 1.0: reuse is a *signal*, not a license to override the extractor's
# judgement. Raising this is a product decision (how much should habitual use
# count compared to an explicit "this is important" at write time).
A_MAX = 0.5

# Reuse events needed to bank half of A_MAX. lambda = ln(2) / N_HALF, so the
# parameter is stated in the unit that is actually being tuned ("how many
# reuses until it is mostly saturated") rather than as a bare rate.
N_HALF = 3.0
LAMBDA = math.log(2.0) / N_HALF

# Half-life of accumulated strength, in days. After this long without reuse the
# banked bonus is halved; after ~5 half-lives it is negligible.
HALF_LIFE_DAYS = 75.0
LAMBDA_T = math.log(2.0) / HALF_LIFE_DAYS

# Events closer together than this add no new gain (only real-time decay). One
# session's repeated mentions collapse into a single reinforcement. Note that
# this is measured from the last event that *counted*, so a burst can never keep
# pushing the clock forward and lock a fact inside the cooldown forever.
COOLDOWN_SEC = 600.0

# The same threshold in milliseconds, which is the unit every timestamp in this
# library uses. Both spellings exist so callers never have to multiply inline —
# a stray `* 1000` (or its absence) is exactly the kind of unit slip that is
# invisible inside a formula.
COOLDOWN_MS = int(COOLDOWN_SEC * 1000)

# Effective counts at which saturated_after() reports the ceiling as reached.
# With N_HALF = 3 the bonus is within 0.1% of A_MAX at n ~ 21, but 1e6 is used
# as the "arbitrarily large" reference so tests can assert the limit behaviour
# without depending on the precision of a particular float.
SATURATION_N = 1e6

MS_PER_DAY = 86_400_000.0

# Reinforcement event kinds and their evidence weights. ``retrieved_only`` is
# recorded for observability but must never strengthen a fact: recall feeding
# recall is the one signal that would make this module self-reinforcing.
KIND_USER_CONFIRMED = "user_confirmed"
KIND_USER_RESTATED = "user_restated"
KIND_APPLIED = "applied"
KIND_RETRIEVED_ONLY = "retrieved_only"

KIND_GAINS: Dict[str, float] = {
    KIND_USER_CONFIRMED: 1.0,
    KIND_USER_RESTATED: 0.8,
    KIND_APPLIED: 0.6,
    KIND_RETRIEVED_ONLY: 0.0,
}


def gain_for(kind: str) -> float:
    """Return the evidence weight of a reinforcement kind.

    Unknown kinds weigh 0.0: an unrecognised signal must never strengthen a
    fact, so new kinds have to be registered here explicitly.

    Args:
        kind: One of the ``KIND_*`` constants.

    Returns:
        The gain in ``[0, 1]``.
    """
    return KIND_GAINS.get(kind, 0.0)


def reinforce_bonus(n: float) -> float:
    """Return the saturating bonus earned by an effective reuse count.

    Implemented with :func:`math.expm1` rather than ``1 - exp(-x)``: once ``n``
    is large the two terms of the naive form are both ~1 and cancel, so the
    result would quantise into repeated values and stop being *strictly*
    increasing. ``-expm1(-x)`` is exactly ``1 - exp(-x)`` computed with the
    small quantity kept relative, which preserves the strict monotonicity the
    design promises all the way into saturation.

    The ceiling is enforced explicitly at the far tail. Past ~n=150 the true
    value is within one ULP of ``A_MAX``, and rounding the product *up* would
    actually return ``A_MAX`` and break the "never reaches the ceiling"
    invariant. Returning the largest float below ``A_MAX`` keeps ``A`` bounded
    by the ceiling for every finite input.

    Args:
        n: Effective reinforcement count (negative values clamp to 0).

    Returns:
        A value in ``[0, A_MAX)``, concave and strictly increasing in ``n``.
    """
    if not n or n <= 0.0:
        return 0.0
    value = -A_MAX * math.expm1(-LAMBDA * n)
    if value >= A_MAX:
        return math.nextafter(A_MAX, 0.0)
    return value


def decay_factor(elapsed_ms: float) -> float:
    """Return the fraction of banked strength left after ``elapsed_ms``.

    The single time-decay primitive in this module. Negative elapsed time (clock
    skew) clamps to ``0`` so a backdated timestamp can never *amplify* strength.

    Args:
        elapsed_ms: Time since the state snapshot was taken, in milliseconds.

    Returns:
        A factor in ``(0, 1]``, halving every ``HALF_LIFE_DAYS``.
    """
    if elapsed_ms <= 0:
        return 1.0
    return math.exp(-LAMBDA_T * (elapsed_ms / MS_PER_DAY))


def adjust(n: float, last_used_at: Optional[int], at: Optional[int] = None) -> float:
    """Decay a state snapshot ``n`` to the instant ``at``.

    This is how a stored snapshot becomes a *current* count. It is the only
    supported way to read ``facts.reinforce_count`` for any purpose that depends
    on strength: the column holds the value as of ``last_used_at``, not now.

    Args:
        n: Banked state snapshot (``facts.reinforce_count``).
        last_used_at: When that snapshot was taken (``facts.last_used_at``).
            ``None`` means no event has ever been banked, so there is nothing to
            decay.
        at: The instant to decay to, in ms; defaults to now.

    Returns:
        The current count, in ``[0, n]``.
    """
    current = float(n or 0.0)
    if current <= 0.0 or not last_used_at:
        return max(0.0, current)
    when = int(at if at is not None else now_ms())
    return current * decay_factor(when - int(last_used_at))


def effective_importance_at(
    base: float, n: float, last_used_at: Optional[int], at: Optional[int] = None
) -> float:
    """Return a fact's *current* effective importance from its raw state.

    The read-time entry point: the stored columns are state, never strength.

    Args:
        base: The ``importance`` stored at write time.
        n: Banked state snapshot (``facts.reinforce_count``).
        last_used_at: When that snapshot was taken (``facts.last_used_at``).
        at: The instant to evaluate at, in ms; defaults to now.

    Returns:
        ``clamp(base + A(adjust(n, last_used_at, at)), 0, 1)``.
    """
    return effective_importance(base, adjust(n, last_used_at, at))


def effective_importance(base: float, n: float) -> float:
    """Combine a base importance with an already-current count.

    Prefer :func:`effective_importance_at` when reading persisted state; this
    variant takes a count that is already current and is what the write path and
    the pure kernel use.

    Args:
        base: The ``importance`` stored at write time.
        n: A *current* reinforcement count (not a stored snapshot).

    Returns:
        ``clamp(base + reinforce_bonus(n), 0, 1)``.
    """
    value = float(base or 0.0) + reinforce_bonus(n)
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def is_saturated(n: float, tolerance: float = 1e-3) -> bool:
    """Whether further reuse is below ``tolerance`` of the ceiling.

    Args:
        n: Effective reinforcement count.
        tolerance: Fraction of ``A_MAX`` considered negligible.

    Returns:
        ``True`` when the remaining headroom is smaller than the tolerance.
    """
    return (A_MAX - reinforce_bonus(n)) <= tolerance * A_MAX


def saturated_after(tolerance: float = 1e-3) -> int:
    """Return the effective count at which reuse stops mattering.

    Useful for reporting and for bounding the useful range of ``n``; the exact
    value follows from ``lambda`` and ``tolerance``.

    Args:
        tolerance: Fraction of ``A_MAX`` considered negligible.

    Returns:
        The smallest integer ``n`` for which :func:`is_saturated` holds.
    """
    if tolerance <= 0.0:
        return 0
    remaining = math.log(max(tolerance, 1e-15)) / -LAMBDA
    return max(0, math.ceil(remaining))


@dataclass(frozen=True)
class RollResult:
    """Outcome of folding one reinforcement event into a fact's state.

    Attributes:
        n: The new effective reinforcement count.
        last_used_at: The new ``last_used_at`` timestamp (ms).
        gain: The gain actually banked (0.0 when the cooldown suppressed it).
        strong_before: The pre-event effective importance.
        strong_after: The post-event effective importance.
        suppressed: Whether the cooldown swallowed the gain.
        applied: Whether the event moved the state at all.
    """

    n: float
    last_used_at: int
    gain: float
    strong_before: float
    strong_after: float
    suppressed: bool
    applied: bool


def roll(
    n: float,
    last_used_at: Optional[int],
    gain: float,
    base: float,
    now: Optional[int] = None,
    cooldown_sec: float = COOLDOWN_SEC,
    event_at: Optional[int] = None,
) -> RollResult:
    """Decay ``n`` to ``now``, then add one reinforcement event's gain.

    Pure: the whole reinforcement aggregate is a function of
    ``(n, last_used_at, gain, base, now)``, which is what makes the stored
    columns replayable from ``fact_reinforcements``.

    Args:
        n: Current effective reinforcement count.
        last_used_at: Timestamp of the last *counted* event, or ``None``.
        gain: The incoming event's evidence weight (see :func:`gain_for`).
        base: The fact's base importance (for the reported strengths only).
        now: Current timestamp in ms; defaults to :func:`~atom_memory.db.now_ms`.
        cooldown_sec: Minimum spacing for a gain to count.
        event_at: Timestamp of the event being folded; defaults to ``now``. The
            decay is measured to this instant so that a backdated event (or a
            replay in chronological order) decays correctly.

    Returns:
        A :class:`RollResult` describing what the event *would* do. Two fields
        carry the decision, and both are the caller's to act on:

        - ``applied``: whether the event passed the cooldown gate. Only an
          applied event may advance the stored snapshot — and ``n`` /
          ``last_used_at`` in the result are meaningful **only** in that case.
        - ``gain``: what it banked (``0.0`` when suppressed, or for a zero-weight
          kind).

        When the event was suppressed, ``n`` is the decayed leftover rather than
        the input state. That value is *not* what should be stored: the snapshot
        must stay exactly as old as its own timestamp claims, so the decay keeps
        being applied from the right origin. Callers that ignore ``applied`` and
        persist ``n`` unconditionally will drift out of agreement with a replay
        of the event log. Decay continues to apply to the untouched snapshot, so
        a flood of duplicates can never *preserve* strength, and because the
        timestamp does not move either, the flood cannot slide the window forward
        to lock the fact out of future reinforcement.
    """
    at = int(event_at if event_at is not None else (now if now is not None else now_ms()))
    current = float(n or 0.0)
    if current < 0.0:
        current = 0.0

    # Guard against clock skew: a negative interval would *amplify* the count.
    #
    # Both the decay and the cooldown are measured against the state's own
    # timestamp (the last event that passed the gate), never against "the last
    # event of any kind". That is what keeps the state replayable: suppression is
    # a pure threshold decision on a stored timestamp, so re-folding the event log
    # reproduces the incremental result exactly.
    interval_ms = at - int(last_used_at) if last_used_at else 0
    if interval_ms < 0:
        interval_ms = 0

    strong_before = effective_importance(base, current)
    decayed = current * decay_factor(interval_ms)

    suppressed = bool(
        last_used_at is not None
        and gain > 0.0
        and cooldown_sec > 0
        and interval_ms < cooldown_sec * 1000.0
    )
    if suppressed:
        new_n = decayed
        banked = 0.0
    else:
        banked = float(gain) if gain > 0.0 else 0.0
        new_n = decayed + banked

    return RollResult(
        n=new_n,
        last_used_at=at,
        gain=banked,
        strong_before=round(strong_before, 6),
        strong_after=round(effective_importance(base, new_n), 6),
        suppressed=suppressed,
        applied=(last_used_at is None) or not suppressed,
    )


# -- persistence --------------------------------------------------------------


def record_reinforcement(
    conn: sqlite3.Connection,
    fact_id: str,
    user_id: str,
    session_id: str,
    kind: str,
    event_at: Optional[int] = None,
) -> Optional[RollResult]:
    """Record one reinforcement event and update the fact's aggregate.

    The event row is inserted first, so the UNIQUE index on
    ``(user_id, session_id, fact_id, kind)`` is what enforces "one
    reinforcement per fact per session per kind" — an ``INSERT OR IGNORE`` that
    changes no row means a duplicate and leaves the aggregate untouched.

    Args:
        conn: Open SQLite connection.
        fact_id: The fact being reinforced.
        user_id: Owner of the fact (hard isolation scope).
        session_id: Session the evidence came from (idempotency scope).
        kind: One of the ``KIND_*`` constants.
        event_at: Event timestamp in ms; defaults to now.

    Returns:
        The :class:`RollResult` that was applied, or ``None`` when the event was
        a duplicate, the kind is not registered in :data:`KIND_GAINS`, or the
        fact does not exist / is not active. A registered kind with gain 0
        (``retrieved_only``) is still recorded for observability, but leaves the
        aggregate untouched.
    """
    if kind not in KIND_GAINS:
        logger.debug("Reinforcement rejected: unregistered kind %r", kind)
        return None

    at = int(event_at if event_at is not None else now_ms())

    row = conn.execute(
        "SELECT importance, reinforce_count, last_used_at FROM facts "
        "WHERE fact_id = ? AND user_id = ? AND status = 'active'",
        (fact_id, user_id),
    ).fetchone()
    if row is None:
        logger.debug("Reinforcement skipped: fact %s not active/found", fact_id)
        return None

    gain = gain_for(kind)

    # Claim the event slot first, so the UNIQUE index on
    # (user_id, session_id, fact_id, kind) is what enforces "one reinforcement
    # per fact per session per kind": an INSERT OR IGNORE that changes no row is
    # a duplicate and leaves the aggregate untouched. The row starts with the
    # nominal gain and is rewritten with the gain actually banked below.
    cursor = conn.execute(
        "INSERT OR IGNORE INTO fact_reinforcements("
        "fact_id, user_id, session_id, kind, gain, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (fact_id, user_id, session_id, kind, gain, at),
    )
    if cursor.rowcount == 0:
        logger.debug(
            "Reinforcement duplicate (%s/%s/%s); aggregate untouched",
            fact_id, session_id, kind,
        )
        return None

    result = roll(
        n=row["reinforce_count"],
        last_used_at=row["last_used_at"],
        gain=gain,
        base=float(row["importance"] or 0.0),
        event_at=at,
    )

    # Three outcomes, and the distinction between the last two is load-bearing:
    #
    #   banked > 0     the event passed the gate and earned something: it becomes
    #                  the new snapshot AND resets the anti-abuse clock.
    #   applied, but   the event passed the gate and earned nothing (a zero-weight
    #   banked == 0    kind such as ``retrieved_only``). It must NOT touch the
    #                  snapshot or its timestamp: advancing the timestamp would
    #                  start a cooldown that a replay — whose gate requires a
    #                  positive gain — would not start, so the two paths would
    #                  disagree.
    #   not applied    the cooldown suppressed it. It must not touch the snapshot
    #                  either, for the same reason, and so the decay keeps
    #                  applying and a duplicate cannot preserve strength.
    #
    # In all three cases the touch is recorded in ``last_seen_at``.
    if result.applied and result.gain > 0.0:
        conn.execute(
            "UPDATE facts SET reinforce_count = ?, last_used_at = ?, "
            "last_seen_at = ? WHERE fact_id = ?",
            (result.n, result.last_used_at, at, fact_id),
        )
    else:
        if not result.applied:
            # Make the discard explicit in the log, so a replay cannot resurrect
            # evidence the cooldown threw away.
            conn.execute(
                "UPDATE fact_reinforcements SET gain = 0 WHERE fact_id = ? "
                "AND user_id = ? AND session_id = ? AND kind = ?",
                (fact_id, user_id, session_id, kind),
            )
        conn.execute(
            "UPDATE facts SET last_seen_at = ? WHERE fact_id = ?", (at, fact_id)
        )
    conn.commit()
    logger.debug(
        "Reinforced %s via %s: n=%.4f strength %.4f -> %.4f%s",
        fact_id, kind, result.n, result.strong_before, result.strong_after,
        " (cooldown: gain dropped)" if result.suppressed else "",
    )
    return result


def rebuild_fact_reinforcement(
    conn: sqlite3.Connection, fact_id: str, user_id: str
) -> Optional[RollResult]:
    """Recompute a fact's reinforcement aggregate from its event log.

    Replays ``fact_reinforcements`` in chronological order. Because ``roll`` is
    purely a function of the prior state — and because both the decay and the
    cooldown are dated from the last event that *counted* — this reproduces
    exactly what the incremental path produced. That is what makes a retuned
    ``A_MAX`` / ``HALF_LIFE_DAYS`` retro-applicable, and any suspected abuse
    correctable, without trusting the stored aggregate.

    Args:
        conn: Open SQLite connection.
        fact_id: The fact to rebuild.
        user_id: Owner of the fact.

    Returns:
        The final :class:`RollResult`, or ``None`` when the fact is missing.
    """
    row = conn.execute(
        "SELECT importance FROM facts WHERE fact_id = ? AND user_id = ?",
        (fact_id, user_id),
    ).fetchone()
    if row is None:
        return None
    base = float(row["importance"] or 0.0)

    events = conn.execute(
        "SELECT kind, gain, created_at FROM fact_reinforcements "
        "WHERE fact_id = ? AND user_id = ? ORDER BY created_at, rowid",
        (fact_id, user_id),
    ).fetchall()

    state: Optional[RollResult] = None
    n, last = 0.0, None
    seen: Optional[int] = None
    for event in events:
        # The stored gain is authoritative: a suppressed event was written with
        # gain 0 *because* it banked nothing, so replaying by kind alone would
        # double-count evidence the cooldown threw away. The cooldown is
        # re-evaluated too, which keeps history faithful to the thresholds.
        gain = (
            float(event["gain"])
            if event["gain"] is not None
            else gain_for(event["kind"])
        )
        at = int(event["created_at"])
        state = roll(
            n=n,
            last_used_at=last,
            gain=gain,
            base=base,
            event_at=at,
        )
        # Only an event that actually banked something advances the snapshot —
        # the exact rule the write path uses. A suppressed event and a
        # gate-passing zero-gain event both leave the count *and* its timestamp
        # where they were, so the next event decays across the whole span and the
        # two paths cannot drift apart.
        if state.applied and state.gain > 0.0:
            n = state.n
            last = state.last_used_at
        seen = at

    conn.execute(
        "UPDATE facts SET reinforce_count = ?, last_used_at = ?, last_seen_at = ? "
        "WHERE fact_id = ?",
        (n, last, seen, fact_id),
    )
    conn.commit()
    if state is None:
        # No events: the fact keeps a zeroed aggregate.
        return roll(n=0.0, last_used_at=None, gain=0.0, base=base)
    return state
