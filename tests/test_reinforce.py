"""Tests for reuse reinforcement (``reinforce.py`` + migration 005).

The point of this module is a *shape*, so the invariant tests come first: the
strengthening must be locally linear, globally bounded, and concave (each extra
reuse counts less than the one before). Everything after that checks the two
things a curve alone cannot give you — that only genuine reuse counts, and that
repeated evidence cannot be manufactured inside one session.
"""

from __future__ import annotations

import asyncio

import pytest

from atom_memory import AtomMem, MemConfig
from atom_memory.db import now_ms
from atom_memory.embedder import serialize_float32
from atom_memory.reinforce import (
    A_MAX,
    COOLDOWN_SEC,
    HALF_LIFE_DAYS,
    KIND_APPLIED,
    KIND_RETRIEVED_ONLY,
    KIND_USER_CONFIRMED,
    KIND_USER_RESTATED,
    LAMBDA,
    SATURATION_N,
    effective_importance,
    gain_for,
    is_saturated,
    record_reinforcement,
    rebuild_fact_reinforcement,
    reinforce_bonus,
    roll,
    saturated_after,
)
from atom_memory.retriever import Retriever

DIM = 512
DAY_MS = 86_400_000
HOUR_MS = 3_600_000
COOLDOWN_MS = int(COOLDOWN_SEC * 1000)
# A birth timestamp far enough in the past that real "now" is well after it,
# and far enough before the event timestamps used below.
T0 = 1_700_000_000_000


# ---- curve invariants -------------------------------------------------------


def test_bonus_is_monotonic_and_bounded():
    """A(n) strictly increases and never reaches A_MAX.

    Strictness is asserted only up to the point where ``double`` can still
    resolve the difference: past ~n=150 the bonus is within 1e-16 of the ceiling
    and consecutive values round to the same float. That is a property of IEEE
    arithmetic, not of the model — by then the curve is saturated to ~17 digits
    and no ranking decision can depend on it.
    """
    values = [reinforce_bonus(n) for n in range(0, 150)]
    assert all(b > a for a, b in zip(values, values[1:])), "not strictly increasing"
    assert values[0] == 0.0, "no reuse must mean no bonus"
    assert all(v < A_MAX for v in values), "bonus escaped its ceiling"
    # Beyond float resolution it is monotone non-decreasing, and still bounded.
    tail = [reinforce_bonus(n) for n in range(150, 300)]
    assert all(b >= a for a, b in zip(tail, tail[1:])), "curve went backwards"
    assert reinforce_bonus(SATURATION_N) < A_MAX
    assert reinforce_bonus(SATURATION_N) > 0.999 * A_MAX


def test_saturation_helpers_agree_with_the_curve():
    """is_saturated / saturated_after describe the same ceiling."""
    n = saturated_after()
    assert not is_saturated(n - 1)
    assert is_saturated(n)
    assert n < 100, "saturation should arrive quickly at N_HALF=3"


def test_bonus_is_concave_so_marginal_effect_decays():
    """Every extra reuse must add strictly less than the previous one."""
    deltas = [
        reinforce_bonus(n + 1) - reinforce_bonus(n) for n in range(0, 50)
    ]
    assert all(
        later < earlier for earlier, later in zip(deltas, deltas[1:])
    ), "marginal effect did not decay"
    assert all(d > 0 for d in deltas), "marginal effect must stay positive"


def test_bonus_is_locally_linear_at_zero():
    """The opening slope is the maximum slope: 1 - e^-x ~ x for small n."""
    slope = reinforce_bonus(1e-6) / 1e-6
    assert slope == pytest.approx(A_MAX * LAMBDA, rel=1e-4)


def test_early_reuses_are_comparable_later_ones_are_not():
    """The concrete shape the design promises, at the tuned parameters."""
    d1 = reinforce_bonus(1) - reinforce_bonus(0)
    d2 = reinforce_bonus(2) - reinforce_bonus(1)
    d3 = reinforce_bonus(3) - reinforce_bonus(2)
    d10 = reinforce_bonus(10) - reinforce_bonus(9)

    # First three reuses are within ~40% of each other: "linear-ish".
    assert d2 > 0.7 * d1
    assert d3 > 0.55 * d1
    # By the tenth the effect is a fraction of the first.
    assert d10 < 0.25 * d1


def test_bonus_matches_closed_form():
    """The implementation is the documented formula, not an approximation."""
    import math

    for n in (0.5, 1.0, 3.0, 7.5):
        assert reinforce_bonus(n) == pytest.approx(
            A_MAX * (1 - math.exp(-LAMBDA * n))
        )


def test_effective_importance_clamps_to_one():
    """A high base fact reinforced heavily cannot exceed 1.0."""
    assert effective_importance(1.0, 100.0) == 1.0
    assert effective_importance(0.9, 5.0) <= 1.0
    assert effective_importance(0.0, 0.0) == 0.0


def test_reinforcement_cannot_invert_a_large_baseline_gap():
    """A_MAX is the whole budget: reuse can never outrank a much better fact."""
    # Worst case for the invariant: zero reuse vs maximal reuse.
    assert effective_importance(0.2, 10_000) <= effective_importance(0.9, 0.0) + 1e-9
    assert effective_importance(0.2, 10_000) < 0.9


def test_gain_for_unknown_kind_is_zero():
    """An unregistered signal must never strengthen a fact."""
    assert gain_for("something_new") == 0.0
    assert gain_for(KIND_RETRIEVED_ONLY) == 0.0
    assert gain_for(KIND_USER_RESTATED) > gain_for(KIND_APPLIED)


# ---- rolling state ----------------------------------------------------------


def test_roll_applies_gain_when_nothing_preceded_it():
    """The very first event always counts."""
    r = roll(n=0.0, last_used_at=None, gain=1.0, base=0.5, event_at=T0)
    assert r.gain == 1.0
    assert r.n == pytest.approx(1.0)
    assert r.suppressed is False
    assert r.strong_after > r.strong_before


def test_roll_decays_by_half_over_a_half_life():
    """Strength decays exponentially with the configured half-life."""
    start = 4.0
    # First bank a count, then measure exactly one half-life later.
    banked = roll(n=0.0, last_used_at=None, gain=start, base=0.0, event_at=T0)
    assert banked.n == pytest.approx(start)
    r = roll(
        n=banked.n,
        last_used_at=banked.last_used_at,
        gain=0.0,
        base=0.0,
        event_at=T0 + int(HALF_LIFE_DAYS * DAY_MS),
    )
    assert r.n == pytest.approx(start / 2.0, rel=1e-6)


def test_roll_ignores_the_age_of_a_first_event():
    """There is nothing to decay from, so the first gain applies in full."""
    r = roll(n=0.0, last_used_at=None, gain=1.0, base=0.0, event_at=T0 + 10 * DAY_MS)
    assert r.n == pytest.approx(1.0)


def test_roll_suppresses_gain_inside_cooldown_but_still_decays():
    """A duplicate in the same session must not add strength."""
    r = roll(
        n=1.0,
        last_used_at=T0,
        gain=1.0,
        base=0.5,
        event_at=T0 + COOLDOWN_MS - 1000,  # just inside the cooldown
    )
    assert r.suppressed is True
    assert r.gain == 0.0
    assert r.n <= 1.0, "a suppressed burst must not increase the count"
    assert r.applied is False
    assert r.strong_after <= r.strong_before


def test_roll_applies_gain_once_the_cooldown_elapsed():
    """Past the cooldown, the evidence counts again."""
    r = roll(
        n=1.0,
        last_used_at=T0,
        gain=1.0,
        base=0.5,
        event_at=T0 + COOLDOWN_MS + 1000,
    )
    assert r.suppressed is False
    assert r.gain == 1.0
    assert r.n > 1.0


def test_roll_never_amplifies_on_clock_skew():
    """A backdated event must not turn into a multiplication."""
    r = roll(n=1.0, last_used_at=10_000_000, gain=1.0, base=0.0, event_at=0)
    assert r.n <= 2.0, "negative interval amplified the count"


def test_roll_repeated_events_cannot_preserve_strength():
    """Back-to-back duplicates each still pay the decay."""
    n = 1.0
    last = T0
    for i in range(1, 6):
        r = roll(n=n, last_used_at=last, gain=1.0, base=0.5, event_at=T0 + 1000 * i)
        n, last = r.n, r.last_used_at
        assert r.suppressed is True
    assert n < 1.0, "a burst of duplicates held the strength up"


def test_roll_burst_starts_the_cooldown_from_the_counted_event():
    """The cooldown is measured from the last event that *counted*.

    The pure function simply reports the event timestamp; what matters is that a
    caller folding state forward keeps the *counted* timestamp (the persistence
    level does — see ``test_suppressed_events_do_not_slide_the_cooldown_clock``).
    So a burst cannot pin a fact inside the cooldown indefinitely.
    """
    banked = roll(n=0.0, last_used_at=None, gain=1.0, base=0.0, event_at=T0)
    # A suppressed event right after it...
    burst = roll(
        n=banked.n,
        last_used_at=banked.last_used_at,
        gain=1.0,
        base=0.0,
        event_at=T0 + 1_000,
    )
    assert burst.suppressed is True and burst.gain == 0.0
    # ...still leaves the cooldown dated from T0, so a real event just after the
    # cooldown applies.
    later = roll(
        n=burst.n, last_used_at=T0, gain=1.0, base=0.0, event_at=T0 + COOLDOWN_MS + 1
    )
    assert later.suppressed is False
    assert later.gain == 1.0


# ---- persistence ------------------------------------------------------------


def _insert_fact(
    conn,
    fact_id: str,
    user_id: str = "u1",
    importance: float = 0.5,
    created_at: int = 1,
):
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, source_type, status, observed_at, "
        "created_at, version) VALUES (?, ?, 's1', '用户', '偏好', '黑咖啡', "
        "0.8, ?, 'user_explicit', 'active', ?, ?, 1)",
        (fact_id, user_id, importance, created_at, created_at),
    )
    conn.commit()


@pytest.fixture()
def conn():
    from atom_memory.db import connect_for_tests

    c = connect_for_tests()
    try:
        yield c
    finally:
        c.close()


def test_record_reinforcement_updates_aggregate(conn):
    """One event moves the aggregate and lands in the event log."""
    _insert_fact(conn, "f1", importance=0.5)
    result = record_reinforcement(conn, "f1", "u1", "s1", KIND_USER_RESTATED)
    assert result is not None
    assert result.gain == pytest.approx(0.8)
    assert result.n == pytest.approx(0.8)

    row = conn.execute(
        "SELECT reinforce_count, last_used_at, last_seen_at FROM facts "
        "WHERE fact_id='f1'"
    ).fetchone()
    assert row["reinforce_count"] == pytest.approx(0.8)
    assert row["last_used_at"] is not None
    # The observability clock tracks events of every kind.
    assert row["last_seen_at"] == row["last_used_at"]
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fact_reinforcements WHERE fact_id='f1'"
    ).fetchone()["n"] == 1


def test_record_reinforcement_is_idempotent_per_session_and_kind(conn):
    """The same claim restated five times in one session yields one event."""
    _insert_fact(conn, "f1")
    first = record_reinforcement(conn, "f1", "u1", "s1", KIND_USER_RESTATED)
    for _ in range(4):
        assert record_reinforcement(conn, "f1", "u1", "s1", KIND_USER_RESTATED) is None
    assert first is not None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fact_reinforcements WHERE fact_id='f1'"
    ).fetchone()["n"] == 1
    # And the rejected duplicates did not quietly strengthen the fact either.
    row = conn.execute(
        "SELECT reinforce_count FROM facts WHERE fact_id='f1'"
    ).fetchone()
    assert row["reinforce_count"] == pytest.approx(0.8)


def test_distinct_kinds_stack_within_one_session(conn):
    """Being confirmed *and* applied in one session is two pieces of evidence.

    They are kept apart by the cooldown (600s), so the two events are an hour
    apart — the same session here is just the idempotency scope, not a claim
    about timing.
    """
    _insert_fact(conn, "f1", created_at=T0)
    first = record_reinforcement(
        conn, "f1", "u1", "s1", KIND_USER_RESTATED, event_at=T0
    )
    second = record_reinforcement(
        conn, "f1", "u1", "s1", KIND_USER_CONFIRMED, event_at=T0 + HOUR_MS
    )
    assert first is not None and second is not None
    assert second.n == pytest.approx(0.8 * 0.9999 + 1.0, rel=1e-3)
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fact_reinforcements WHERE fact_id='f1'"
    ).fetchone()["n"] == 2


def test_events_inside_the_cooldown_add_nothing_but_are_logged(conn):
    """Two kinds back to back in one session: the second buys no strength."""
    _insert_fact(conn, "f1", created_at=T0)
    record_reinforcement(conn, "f1", "u1", "s1", KIND_USER_RESTATED, event_at=T0)
    second = record_reinforcement(
        conn, "f1", "u1", "s1", KIND_USER_CONFIRMED, event_at=T0 + 1_000
    )
    assert second is not None
    assert second.suppressed is True
    assert second.gain == 0.0
    row = conn.execute(
        "SELECT reinforce_count, last_used_at, last_seen_at FROM facts "
        "WHERE fact_id='f1'"
    ).fetchone()
    assert row["reinforce_count"] <= 0.8
    # Observability still records the suppressed touch...
    assert row["last_seen_at"] == T0 + 1_000
    # ...while the maths still dates from the event that counted.
    assert row["last_used_at"] == T0


def test_same_kind_from_a_new_session_strengthens_again(conn):
    """Reuse across sessions is real reuse (once the cooldown has passed)."""
    _insert_fact(conn, "f1", created_at=T0)
    first = record_reinforcement(
        conn, "f1", "u1", "s1", KIND_USER_RESTATED, event_at=T0
    )
    later = record_reinforcement(
        conn, "f1", "u1", "s2", KIND_USER_RESTATED, event_at=T0 + HOUR_MS
    )
    assert first is not None and later is not None
    assert later.suppressed is False
    assert later.n > 0.8


def test_retrieved_only_never_strengthens(conn):
    """Recall must not feed back into the score.

    The event is still logged (it is useful to see that a fact *was* recalled),
    but it must leave the aggregate exactly where it was.
    """
    _insert_fact(conn, "f1")
    record_reinforcement(conn, "f1", "u1", "s1", KIND_RETRIEVED_ONLY)
    row = conn.execute("SELECT reinforce_count FROM facts WHERE fact_id='f1'").fetchone()
    assert row["reinforce_count"] == 0.0
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fact_reinforcements WHERE fact_id='f1'"
    ).fetchone()["n"] == 1


def test_unknown_kind_is_rejected_without_side_effects(conn):
    """An unregistered kind writes no event at all."""
    _insert_fact(conn, "f1")
    assert record_reinforcement(conn, "f1", "u1", "s1", "made_up") is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fact_reinforcements"
    ).fetchone()["n"] == 0


def test_reinforcement_is_scoped_to_the_fact_owner(conn):
    """Another user's fact id must not be strengthened."""
    _insert_fact(conn, "f1", user_id="u1")
    assert record_reinforcement(conn, "f1", "u_other", "s1", KIND_APPLIED) is None
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM fact_reinforcements"
    ).fetchone()["n"] == 0


def test_reinforcement_respects_user_isolation_of_reads(conn):
    """Facts, not just events, are isolated: a foreign id does not resolve."""
    _insert_fact(conn, "f1", user_id="u2")
    assert record_reinforcement(conn, "f1", "u1", "s1", KIND_APPLIED) is None


def test_inactive_fact_is_not_reinforced(conn):
    """A retracted/superseded fact must not accumulate strength."""
    _insert_fact(conn, "f1")
    conn.execute("UPDATE facts SET status='retracted' WHERE fact_id='f1'")
    conn.commit()
    assert record_reinforcement(conn, "f1", "u1", "s1", KIND_APPLIED) is None


def test_rebuild_reproduces_the_incremental_aggregate(conn):
    """The stored aggregate is exactly replayable from the event log.

    This is what makes the whole mechanism tunable and auditable: change A_MAX
    or the half-life, replay, and you get the honest answer for the recorded
    evidence. Four sessions spread a day apart, so the gain applies each time
    while the accumulated strength also decays in between.
    """
    _insert_fact(conn, "f1", importance=0.4, created_at=T0 - DAY_MS)
    kinds = [KIND_USER_RESTATED, KIND_USER_CONFIRMED, KIND_APPLIED, KIND_USER_RESTATED]
    for i, kind in enumerate(kinds):
        record_reinforcement(
            conn, "f1", "u1", f"s{i}", kind, event_at=T0 + i * DAY_MS
        )

    incremental = conn.execute(
        "SELECT reinforce_count, last_used_at, last_seen_at FROM facts "
        "WHERE fact_id='f1'"
    ).fetchone()
    assert incremental["reinforce_count"] > 3.0

    conn.execute(
        "UPDATE facts SET reinforce_count = 0, last_used_at = NULL, "
        "last_seen_at = NULL"
    )
    conn.commit()
    rebuilt = rebuild_fact_reinforcement(conn, "f1", "u1")

    assert rebuilt is not None
    assert rebuilt.n == pytest.approx(incremental["reinforce_count"], rel=1e-9)
    assert rebuilt.last_used_at == incremental["last_used_at"]
    assert conn.execute(
        "SELECT last_seen_at FROM facts WHERE fact_id='f1'"
    ).fetchone()["last_seen_at"] == incremental["last_seen_at"]


def test_rebuild_handles_a_suppressed_event_faithfully(conn):
    """A cooldown-suppressed event must replay to the same number.

    Suppressed events are logged but discarded, and both the decay and the
    cooldown are dated from the last event that *counted*. So the replay must
    ignore them as well - otherwise the rebuilt aggregate would silently
    disagree with the live one, and the "replay after retuning" promise would be
    false exactly where it matters most (abuse investigation).
    """
    _insert_fact(conn, "f1", importance=0.5, created_at=T0 - DAY_MS)
    # s1 counts; s2 and s3 fall inside the cooldown of s1; s4 is a day later.
    assert record_reinforcement(
        conn, "f1", "u1", "s1", KIND_USER_RESTATED, event_at=T0
    ) is not None
    s2 = record_reinforcement(
        conn, "f1", "u1", "s2", KIND_APPLIED, event_at=T0 + 1_000
    )
    s3 = record_reinforcement(
        conn, "f1", "u1", "s3", KIND_USER_CONFIRMED, event_at=T0 + 2_000
    )
    s4 = record_reinforcement(
        conn, "f1", "u1", "s4", KIND_USER_RESTATED, event_at=T0 + DAY_MS
    )
    assert s2.suppressed is True and s2.gain == 0.0
    assert s3.suppressed is True and s3.gain == 0.0
    assert s4.suppressed is False and s4.gain == pytest.approx(0.8)

    # Exactly two of the four events banked anything.
    expected = 0.8 * 2.0 ** (-1.0 / HALF_LIFE_DAYS) + 0.8
    assert s4.n == pytest.approx(expected, rel=1e-9)

    live = conn.execute(
        "SELECT reinforce_count, last_used_at, last_seen_at FROM facts "
        "WHERE fact_id='f1'"
    ).fetchone()
    # The cooldown clock never moved past the last counted event (s4), while
    # observability recorded the most recent touch of any kind.
    assert live["last_used_at"] == T0 + DAY_MS
    assert live["last_seen_at"] == T0 + DAY_MS

    conn.execute(
        "UPDATE facts SET reinforce_count = 0, last_used_at = NULL, "
        "last_seen_at = NULL"
    )
    conn.commit()
    rebuilt = rebuild_fact_reinforcement(conn, "f1", "u1")

    assert rebuilt.n == pytest.approx(live["reinforce_count"], rel=1e-9)
    assert rebuilt.last_used_at == live["last_used_at"]


def test_suppressed_events_do_not_lock_out_future_reinforcement(conn):
    """A chatty session must not be able to pin a fact inside the cooldown.

    The cooldown is measured from the last event that *counted*, so a flurry of
    discarded duplicates cannot keep restarting the 600s window and deny the
    fact any future reinforcement.
    """
    _insert_fact(conn, "f1", created_at=T0)
    first = record_reinforcement(
        conn, "f1", "u1", "s1", KIND_USER_RESTATED, event_at=T0
    )
    assert first.applied is True
    # A flurry of duplicates, a minute apart, all inside the cooldown of the
    # *counted* event.
    for i in range(1, 5):
        suppressed = record_reinforcement(
            conn, "f1", "u1", f"s_burst{i}", KIND_APPLIED,
            event_at=T0 + i * 60 * 1000,
        )
        assert suppressed is not None and suppressed.suppressed is True
        assert suppressed.gain == 0.0

    row = conn.execute(
        "SELECT reinforce_count, last_used_at, last_seen_at FROM facts "
        "WHERE fact_id='f1'"
    ).fetchone()
    # The count is untouched by unbanked evidence...
    assert row["reinforce_count"] == pytest.approx(0.8)
    # ...the cooldown clock still dates from the counted event...
    assert row["last_used_at"] == T0
    # ...and observability saw the most recent touch.
    assert row["last_seen_at"] == T0 + 4 * 60 * 1000
    # Every discarded event is still logged, with an honest gain of 0.
    gains = [
        r["gain"]
        for r in conn.execute(
            "SELECT gain FROM fact_reinforcements WHERE kind = ? ORDER BY created_at",
            (KIND_APPLIED,),
        ).fetchall()
    ]
    assert gains == [0.0, 0.0, 0.0, 0.0]

    # 11 minutes after the counted event, a real restatement still lands.
    applied = record_reinforcement(
        conn, "f1", "u1", "s_real", KIND_USER_RESTATED, event_at=T0 + 11 * 60 * 1000
    )
    assert applied is not None
    assert applied.suppressed is False
    assert applied.gain == pytest.approx(0.8)


def test_rebuild_without_events_zeroes_the_aggregate(conn):
    """A fact nobody reused keeps a zeroed aggregate."""
    _insert_fact(conn, "f1")
    result = rebuild_fact_reinforcement(conn, "f1", "u1")
    assert result is not None
    assert result.n == 0.0
    row = conn.execute(
        "SELECT reinforce_count FROM facts WHERE fact_id='f1'"
    ).fetchone()
    assert row["reinforce_count"] == 0.0


def test_rebuild_returns_none_for_unknown_fact(conn):
    assert rebuild_fact_reinforcement(conn, "nope", "u1") is None


# ---- ranking ----------------------------------------------------------------


def test_rerank_orders_reinforced_facts_first():
    """The reinforcement bonus must decide the ranking, not just decorate it.

    Unit-level on purpose: ``search`` fuses FTS and vector results, and RRF —
    like every other term — is min-max normalised per query, so *any* tiny
    relevance gap between two candidates is inflated into a full 0.4 weight and
    would swamp the 0.1 the reinforcement budget can contribute. Feeding
    ``_rerank`` equal RRF and equal age isolates the term under test.
    """
    from atom_memory.db import connect_for_tests

    conn = connect_for_tests()
    try:
        for fact_id in ("f_used", "f_idle"):
            conn.execute(
                "INSERT INTO facts(fact_id, user_id, session_id, subject, "
                "predicate, object, confidence, importance, source_type, status, "
                "observed_at, created_at, version) "
                "VALUES (?, 'u1', 's1', '用户', '偏好', '黑咖啡', 0.8, 0.5, "
                "'user_explicit', 'active', 1, 1, 1)",
                (fact_id,),
            )
        conn.commit()
        record_reinforcement(
            conn, "f_used", "u1", "s1", KIND_USER_RESTATED, event_at=T0
        )
        record_reinforcement(
            conn, "f_used", "u1", "s2", KIND_USER_CONFIRMED, event_at=T0 + HOUR_MS
        )

        retriever = Retriever(conn, lambda text: serialize_float32([1.0] * DIM))
        facts = retriever._fetch_facts("u1", ["f_used", "f_idle"])
        assert [f["fact_id"] for f in facts] == ["f_used", "f_idle"]
        ranked = retriever._rerank(facts, {"f_used": 1.0, "f_idle": 1.0})

        assert [f["fact_id"] for f in ranked] == ["f_used", "f_idle"]
        used, idle = ranked
        assert used["importance"] == pytest.approx(0.5), "base must be preserved"
        assert used["effective_importance"] > idle["effective_importance"]
        assert used["final_score"] > idle["final_score"]
    finally:
        conn.close()


def test_retrieval_carries_the_reinforced_strength():
    """``search`` reports the effective importance alongside the base value."""
    from atom_memory.db import connect_for_tests
    from atom_memory.retriever import segment_text

    conn = connect_for_tests()
    try:
        for fact_id, obj in (("f_used", "黑咖啡"), ("f_idle", "奶茶")):
            conn.execute(
                "INSERT INTO facts(fact_id, user_id, session_id, subject, "
                "predicate, object, confidence, importance, source_type, status, "
                "observed_at, created_at, version) "
                "VALUES (?, 'u1', 's1', '用户', '偏好', ?, 0.8, 0.5, "
                "'user_explicit', 'active', 1, 1, 1)",
                (fact_id, obj),
            )
            conn.execute(
                "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
                (fact_id, " ".join(segment_text(f"用户 偏好 {obj}"))),
            )
            conn.execute(
                "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
                (fact_id, serialize_float32([1.0] * DIM)),
            )
        conn.commit()
        record_reinforcement(
            conn, "f_used", "u1", "s1", KIND_USER_RESTATED, event_at=T0
        )

        retriever = Retriever(conn, lambda text: serialize_float32([1.0] * DIM))
        ranked = asyncio.run(retriever.search("u1", "用户偏好", top_k=5))
        by_id = {f["fact_id"]: f for f in ranked}
        assert set(by_id) == {"f_used", "f_idle"}
        assert by_id["f_used"]["importance"] == pytest.approx(0.5)
        assert by_id["f_used"]["effective_importance"] > 0.5
        assert by_id["f_idle"]["effective_importance"] == pytest.approx(0.5)
        assert "final_score" in by_id["f_used"]
        assert "recency" in by_id["f_used"]
    finally:
        conn.close()


# ---- recency ----------------------------------------------------------------


def test_recency_credit_decays_by_half_life():
    """The shared decay helper is a plain half-life curve, in caller units."""
    from atom_memory.db import recency_credit

    assert recency_credit(0, 100) == 1.0
    assert recency_credit(100, 100) == pytest.approx(0.5)
    assert recency_credit(200, 100) == pytest.approx(0.25)
    # A reference offset is a floor on the credit, not a subtraction that can
    # push a newer item above 1.0.
    assert recency_credit(0, 100, reference_offset=50) == 1.0
    assert recency_credit(150, 100, reference_offset=50) == pytest.approx(0.5)
    with pytest.raises(ValueError):
        recency_credit(1, 0)


def test_age_offset_shifts_to_the_newest_and_caps_the_shift():
    """Ages are made relative to the newest item, and the shift is bounded."""
    from atom_memory.db import age_offset

    # Relative: the newest becomes 0, so the credit it earns is exactly 1.0.
    assert age_offset(1000, 1000, 5000) == 0.0
    assert age_offset(1500, 1000, 5000) == pytest.approx(500.0)
    # Negative offsets (a "newer" item, i.e. clock skew) clamp to 0.
    assert age_offset(900, 1000, 5000) == 0.0
    # The shift is capped, so an already-old set cannot saturate: with a 2000
    # window, 10_000 of extra age still reads as 2000, not 10_000.
    assert age_offset(11_000, 1_000, 2000) == pytest.approx(2000.0)
    with pytest.raises(ValueError):
        age_offset(1.0, 0.0, 0.0)


def _recency_case(facts):
    """Build a set of facts and return ``{fact_id: recency}`` plus the ranking."""
    from atom_memory.db import connect_for_tests

    conn = connect_for_tests()
    try:
        now = now_ms()
        for fact_id, age_days in facts:
            created = now - int(age_days * DAY_MS)
            conn.execute(
                "INSERT INTO facts(fact_id, user_id, session_id, subject, "
                "predicate, object, confidence, importance, source_type, status, "
                "observed_at, created_at, version) "
                "VALUES (?, 'u1', 's1', '用户', '偏好', ?, 0.8, 0.5, "
                "'user_explicit', 'active', ?, ?, 1)",
                (fact_id, fact_id, created, created),
            )
        conn.commit()
        retriever = Retriever(conn, lambda text: serialize_float32([1.0] * DIM))
        ids = [f[0] for f in facts]
        rows = retriever._fetch_facts("u1", ids)
        scores = {fid: 1.0 for fid in ids}
        ranked = retriever._rerank(rows, scores)
        return {f["fact_id"]: f["recency"] for f in ranked}, ranked
    finally:
        conn.close()


def test_recency_does_not_stretch_milliseconds_into_a_full_range():
    """The failure this replaced: min-max called a millisecond "maximally old".

    A set of same-session facts differs by milliseconds. Min-max would hand the
    newest 1.0 and the oldest 0.0 — the full 0.2 recency weight spent on a
    difference nobody can perceive. An absolute decay keeps them all ~1.0.
    """
    recency, _ = _recency_case([
        ("f_newest", 0.0),
        ("f_second", 0.001),   # ~86 seconds earlier
        ("f_third", 1.0 / 1440),  # one minute earlier
    ])
    assert recency["f_newest"] == pytest.approx(1.0)
    # Every member stays in the top decile: the spread is small because the real
    # age spread is small.
    assert min(recency.values()) > 0.9
    assert max(recency.values()) - min(recency.values()) < 0.01


def test_recency_still_discriminates_real_age_gaps():
    """But a genuine gap must still move the score, or recency is dead weight."""
    recency, ranked = _recency_case([
        ("f_today", 0.0),
        ("f_month", 30.0),
        ("f_quarter", 90.0),
    ])
    assert recency["f_today"] == pytest.approx(1.0)
    # 30 days == the half-life, so ~0.5; 90 days == three half-lives, ~0.125.
    assert recency["f_month"] == pytest.approx(0.5, abs=0.01)
    assert recency["f_quarter"] == pytest.approx(0.125, abs=0.01)
    # All else equal, recency orders them (by driving final_score, which is the
    # only term that decides the ranking).
    assert [f["fact_id"] for f in ranked] == ["f_today", "f_month", "f_quarter"]


def test_recency_window_does_not_flatten_real_age_differences():
    """The shift cap must stay above the half-life, or it erases recency.

    Regression guard: with ``window == half_life`` every candidate more than one
    half-life older than the newest is clamped to the same offset and therefore
    the same credit, so 30-day-old and 90-day-old facts became indistinguishable.
    """
    from atom_memory.retriever import (
        RECENCY_HALF_LIFE_DAYS,
        RECENCY_REFERENCE_WINDOW_DAYS,
    )

    assert RECENCY_REFERENCE_WINDOW_DAYS >= 3 * RECENCY_HALF_LIFE_DAYS
    recency, _ = _recency_case([
        ("f_today", 0.0),
        ("f_month", 30.0),
        ("f_quarter", 90.0),
    ])
    assert len(set(recency.values())) == 3, "age differences were flattened away"


def test_recency_does_not_collapse_when_everything_is_old():
    """An all-old set must still spread, not read as "all maximally stale".

    This is the case a naive absolute decay gets wrong: measuring against the
    wall clock would score every fact ~0 and silently switch the recency term
    off. Clamping the reference to a window keeps the relative order meaningful.
    """
    recency, _ = _recency_case([
        ("f_older", 400.0),
        ("f_old", 430.0),
        ("f_oldest", 460.0),
    ])
    # The newest of the set anchors the window, so it scores full credit...
    assert recency["f_older"] == pytest.approx(1.0)
    # ...and the genuinely older ones are still distinguished from it.
    assert recency["f_old"] < 0.6
    assert recency["f_oldest"] < 0.4


def test_recency_prefers_the_last_used_over_creation():
    """A long-lived fact still in use must not be aged out for being old.

    Reuse is what makes a memory current, so the age basis is
    ``COALESCE(last_used_at, created_at)``. Without this, reinforcement and
    recency would cancel each other out for exactly the facts that earned it.
    """
    from atom_memory.db import connect_for_tests

    conn = connect_for_tests()
    try:
        now = now_ms()
        old = now - int(200 * DAY_MS)
        for fact_id in ("f_reused", "f_dormant"):
            conn.execute(
                "INSERT INTO facts(fact_id, user_id, session_id, subject, "
                "predicate, object, confidence, importance, source_type, status, "
                "observed_at, created_at, version) "
                "VALUES (?, 'u1', 's1', '用户', '偏好', '黑咖啡', 0.8, 0.5, "
                "'user_explicit', 'active', ?, ?, 1)",
                (fact_id, old, old),
            )
        conn.commit()
        # The reused one was touched an hour ago; its created_at stays old.
        record_reinforcement(
            conn, "f_reused", "u1", "s1", KIND_USER_RESTATED,
            event_at=now - int(HOUR_MS),
        )
        retriever = Retriever(conn, lambda text: serialize_float32([1.0] * DIM))
        rows = retriever._fetch_facts("u1", ["f_reused", "f_dormant"])
        ranked = retriever._rerank(rows, {"f_reused": 1.0, "f_dormant": 1.0})
        by_id = {f["fact_id"]: f for f in ranked}
        # The reused fact is an hour old as far as recency is concerned...
        assert by_id["f_reused"]["recency"] > 0.9
        # ...while the dormant one sits on the window floor: 200 days of real
        # age cannot decay below 2**-3, because the shift is capped at three
        # half-lives. The floor is what keeps an all-old set from collapsing to
        # zero, and it is deliberately a *floor*, not a zero.
        assert by_id["f_dormant"]["recency"] == pytest.approx(0.125, abs=0.01)
        assert ranked[0]["fact_id"] == "f_reused"
    finally:
        conn.close()


# ---- API integration --------------------------------------------------------


class _FakeEmbedder:
    def __init__(self, **kwargs) -> None:
        pass

    def embed_one(self, text: str) -> bytes:
        return serialize_float32([0.5] * 512)


def _make(monkeypatch) -> AtomMem:
    """Build an in-memory AtomMem with a deterministic fake embedder.

    ``:memory:`` (rather than a tmp_path file) keeps these tests runnable in
    sandboxes that deny writes outside the workspace; the DB is per-connection
    and these scenarios never reopen it.
    """
    monkeypatch.setattr("atom_memory.api.Embedder", _FakeEmbedder)
    return AtomMem(
        MemConfig(
            db_path=":memory:",
            worker_poll_interval_sec=0.05,
            max_retries=3,
        )
    )


def _run(coro):
    return asyncio.run(coro)


def _insert_api_fact(mem, fact_id: str, importance: float = 0.5):
    mem.db.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, source_type, status, observed_at, "
        "created_at, version) VALUES (?, 'u1', 's1', '用户', '偏好', '黑咖啡', "
        "0.8, ?, 'user_explicit', 'active', 1, 1, 1)",
        (fact_id, importance),
    )
    mem.db.commit()


def test_api_reinforce_strengthens_and_is_idempotent(monkeypatch):
    """The public entry point strengthens once per session and reports it."""
    mem = _make(monkeypatch)

    async def scenario():
        await mem.start()
        _insert_api_fact(mem, "f1")
        first = mem.reinforce("u1", "f1", KIND_USER_CONFIRMED, session_id="s_a")
        again = mem.reinforce("u1", "f1", KIND_USER_CONFIRMED, session_id="s_a")
        # The cooldown is per fact, not per kind, so backdate the last counted
        # event to model the hour of real time that would have to pass before
        # further evidence can bank anything.
        mem.db.execute(
            "UPDATE facts SET last_used_at = last_used_at - ?",
            (HOUR_MS,),
        )
        mem.db.commit()
        other = mem.reinforce("u1", "f1", KIND_APPLIED, session_id="s_b")
        listed = mem.list_facts("u1")["facts"][0]
        await mem.stop()
        return first, again, other, listed

    first, again, other, listed = _run(scenario())
    assert first["applied"] is True
    assert first["importance"] == pytest.approx(0.5)
    assert first["effective_importance"] > first["importance"]
    assert again["applied"] is False and again["gain"] == 0.0
    assert other["applied"] is True
    assert other["reinforce_count"] > first["reinforce_count"]
    assert listed["reinforce_count"] == pytest.approx(other["reinforce_count"])
    assert listed["effective_importance"] == pytest.approx(
        other["effective_importance"], abs=1e-6
    )


def test_api_reinforce_respects_the_cooldown(monkeypatch):
    """Evidence arriving inside the cooldown is logged but banks nothing.

    ``select 1`` as the fake clock: the API calls ``record_reinforcement``
    without a timestamp, which reads ``now_ms()`` from the module it lives in.
    """
    mem = _make(monkeypatch)

    async def scenario():
        await mem.start()
        _insert_api_fact(mem, "f1")
        base = now_ms()
        # Two sessions, same kind, one second apart: the second is inside the
        # 600s cooldown, so it cannot be a way to farm strength.
        monkeypatch.setattr(
            "atom_memory.reinforce.now_ms", lambda: base, raising=True
        )
        first = mem.reinforce("u1", "f1", KIND_USER_RESTATED, session_id="s_a")
        monkeypatch.setattr(
            "atom_memory.reinforce.now_ms", lambda: base + 1_000, raising=True
        )
        second = mem.reinforce("u1", "f1", KIND_USER_RESTATED, session_id="s_b")
        events = mem.db.execute(
            "SELECT session_id, gain FROM fact_reinforcements "
            "WHERE user_id='u1' ORDER BY created_at"
        ).fetchall()
        await mem.stop()
        return first, second, events

    first, second, events = _run(scenario())
    assert first["applied"] is True
    assert second["applied"] is False and second["gain"] == 0.0
    assert second["reinforce_count"] == pytest.approx(first["reinforce_count"])
    assert [e["session_id"] for e in events] == ["s_a", "s_b"]
    assert [e["gain"] for e in events] == [0.8, 0.0]


def test_api_reinforce_rejects_unknown_fact(monkeypatch):
    """A missing/foreign fact is an error, not a silent no-op."""
    mem = _make(monkeypatch)

    async def scenario():
        await mem.start()
        try:
            with pytest.raises(ValueError):
                mem.reinforce("u1", "does_not_exist")
            return True
        finally:
            await mem.stop()

    assert _run(scenario()) is True


def test_editing_a_fact_does_not_reinforce_it(monkeypatch):
    """A UI edit is not evidence of reuse, so it must not strengthen a fact.

    An edit can be a reword, a type fix, or the correction of a *wrong* memory —
    the last of which is evidence against it. Treating every write as a
    confirmation let the settings panel mint the strongest signal (gain 1.0) for
    free; callers that mean "the user confirmed this" say so explicitly.
    """
    mem = _make(monkeypatch)

    async def scenario():
        await mem.start()
        _insert_api_fact(mem, "f1")
        updated = await mem.edit_fact("u1", "f1", object="手冲咖啡")
        explicit = mem.reinforce("u1", "f1", KIND_USER_CONFIRMED, session_id="s_ui")
        listed = mem.list_facts("u1")["facts"][0]
        await mem.stop()
        return updated, explicit, listed

    updated, explicit, listed = _run(scenario())
    assert updated["object"] == "手冲咖啡"
    assert updated["reinforce_count"] == 0.0, "an edit banked strength"
    assert updated["effective_importance"] == pytest.approx(updated["importance"])
    # The explicit call is the supported path, and it works as documented.
    assert explicit["applied"] is True
    assert explicit["gain"] == pytest.approx(1.0)
    assert listed["reinforce_count"] == pytest.approx(1.0)


def test_restating_a_claim_in_a_new_session_reinforces_it(monkeypatch):
    """The implicit half of the loop: duplicated extraction is reuse evidence."""
    mem = _make(monkeypatch)

    async def scenario():
        await mem.start()
        await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.8)
        await mem.add("u1", "s2", "用户喜欢黑咖啡", turn_id=1)
        await asyncio.sleep(0.8)
        rows = mem.db.execute(
            "SELECT reinforce_count FROM facts WHERE user_id='u1' "
            "AND status='active'"
        ).fetchall()
        events = mem.db.execute(
            "SELECT kind FROM fact_reinforcements WHERE user_id='u1'"
        ).fetchall()
        await mem.stop()
        return rows, events

    rows, events = _run(scenario())
    assert len(rows) == 1, "the duplicate must still not be written twice"
    assert rows[0]["reinforce_count"] > 0.0, "restatement did not reinforce"
    assert [e["kind"] for e in events] == [KIND_USER_RESTATED]


def test_restating_within_one_session_counts_once(monkeypatch):
    """Same session, many turns: exactly one reinforcement event."""
    mem = _make(monkeypatch)

    async def scenario():
        await mem.start()
        for turn in range(1, 4):
            await mem.add("u1", "s1", "用户喜欢黑咖啡", turn_id=turn)
            await asyncio.sleep(0.5)
        events = mem.db.execute(
            "SELECT COUNT(*) AS n FROM fact_reinforcements WHERE user_id='u1'"
        ).fetchone()["n"]
        row = mem.db.execute(
            "SELECT reinforce_count FROM facts WHERE user_id='u1' "
            "AND status='active'"
        ).fetchone()
        await mem.stop()
        return events, row

    events, row = _run(scenario())
    assert events == 1
    assert row["reinforce_count"] == pytest.approx(0.8)


def test_last_used_at_column_defaults_to_null():
    """Pre-existing facts start un-reinforced: strength equals base."""
    from atom_memory.db import connect_for_tests

    conn = connect_for_tests()
    try:
        obj = now_ms()
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, observed_at, created_at) VALUES ('f1','u1','s1','用户',"
            "'偏好','黑咖啡',?,?)",
            (obj, obj),
        )
        conn.commit()
        row = conn.execute(
            "SELECT reinforce_count, last_used_at, last_seen_at FROM facts "
            "WHERE fact_id='f1'"
        ).fetchone()
        assert row["reinforce_count"] == 0.0
        assert row["last_used_at"] is None
        assert row["last_seen_at"] is None
    finally:
        conn.close()
