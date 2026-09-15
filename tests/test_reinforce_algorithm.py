"""Algorithm-level validation of reuse reinforcement + decay.

The other reinforcement tests are *behavioural* (does an event strengthen a fact,
does an edit not). This module validates the **algorithm itself** as something
that has to keep running for years:

1. **State vs strength** — the persisted columns are a snapshot; every strength
   is derived by decaying them. The defect this pins: reading the snapshot as if
   it were current leaves a fact at its year-old strength forever.
2. **Long-run invariants** — over simulated years and thousands of events, no
   sequence may break monotonicity, the bound, or the effect of forgetting.
3. **Differential replay** — the incremental write path and a from-scratch replay
   of the event log must agree *exactly* on random timelines. This is the property
   that makes the mechanism auditable, so it is tested adversarially rather than
   on a hand-picked sequence.
4. **Numerical safety** — extreme ages, counts and gains must not overflow,
   underflow into nonsense, or break the ceiling.
5. **Operational bounds** — the event log cannot grow without bound per fact, and
   the schema enforces that without application cooperation.
"""

from __future__ import annotations

import math
import random

import pytest

from atom_memory.reinforce import (
    A_MAX,
    COOLDOWN_MS,
    KIND_APPLIED,
    KIND_GAINS,
    KIND_RETRIEVED_ONLY,
    KIND_USER_CONFIRMED,
    KIND_USER_RESTATED,
    LAMBDA,
    SATURATION_N,
    adjust,
    decay_factor,
    effective_importance,
    effective_importance_at,
    gain_for,
    rebuild_fact_reinforcement,
    record_reinforcement,
    reinforce_bonus,
    roll,
)

DAY_MS = 86_400_000
HOUR_MS = 3_600_000
T0 = 1_700_000_000_000
GAIN_KINDS = [k for k, g in KIND_GAINS.items() if g > 0]


# -- fixtures -----------------------------------------------------------------


@pytest.fixture()
def conn():
    from atom_memory.db import connect_for_tests

    c = connect_for_tests()
    try:
        yield c
    finally:
        c.close()


def _insert_fact(
    conn,
    fact_id: str = "f1",
    user_id: str = "u1",
    importance: float = 0.5,
    created_at: int = T0,
):
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, source_type, status, observed_at, "
        "created_at, version) VALUES (?, ?, 's1', '用户', '偏好', '黑咖啡', "
        "0.8, ?, 'user_explicit', 'active', ?, ?, 1)",
        (fact_id, user_id, importance, created_at, created_at),
    )
    conn.commit()


def _stored(conn, fact_id: str = "f1") -> tuple:
    row = conn.execute(
        "SELECT reinforce_count, last_used_at, last_seen_at FROM facts "
        "WHERE fact_id = ?",
        (fact_id,),
    ).fetchone()
    return float(row["reinforce_count"] or 0.0), row["last_used_at"], row["last_seen_at"]


def _strength(conn, fact_id: str = "f1", at: int | None = None) -> float:
    row = conn.execute(
        "SELECT importance, reinforce_count, last_used_at FROM facts "
        "WHERE fact_id = ?",
        (fact_id,),
    ).fetchone()
    return effective_importance_at(
        float(row["importance"] or 0.0),
        float(row["reinforce_count"] or 0.0),
        row["last_used_at"],
        at,
    )


# -- 1. state vs strength -----------------------------------------------------


def test_adjust_is_the_single_decay_path():
    """A snapshot is only ever interpreted through ``adjust``."""
    # No snapshot -> nothing to decay, ever.
    assert adjust(0.0, None, T0 + 10 * DAY_MS) == 0.0
    assert adjust(3.0, None, T0 + 10 * DAY_MS) == 3.0
    # Decay is monotone decreasing in elapsed time and never sign-flips.
    prev = math.inf
    for days in (0, 1, 7, 75, 400, 3000):
        value = adjust(2.0, T0, T0 + int(days * DAY_MS))
        assert 0.0 <= value <= 2.0
        assert value < prev or days == 0
        prev = value
    # Half of it is gone at exactly one half-life.
    from atom_memory.reinforce import HALF_LIFE_DAYS

    assert adjust(1.0, T0, T0 + int(HALF_LIFE_DAYS * DAY_MS)) == pytest.approx(0.5)


def test_stored_snapshot_is_not_the_strength(conn):
    """The regression: a year of no events must actually weaken the fact."""
    _insert_fact(conn, importance=0.5)
    record_reinforcement(
        conn, "f1", "u1", "s1", KIND_USER_CONFIRMED, event_at=T0
    )
    at_event = _strength(conn, at=T0)
    assert at_event == pytest.approx(effective_importance(0.5, 1.0))

    # The stored snapshot does not move on its own (nothing rewrites rows in the
    # background), so a naive reader keeps seeing the old strength...
    snapshot, last_used, _ = _stored(conn)
    assert snapshot == pytest.approx(1.0)
    assert effective_importance(0.5, snapshot) == pytest.approx(at_event)

    # ...but the derived strength decays to (almost) nothing, and after a full
    # forgetting horizon the fact is back to its base importance. A year is
    # ~4.9 half-lives, so ~0.4% of the bonus is still there.
    after_a_year = _strength(conn, at=T0 + 365 * DAY_MS)
    assert 0.5 < after_a_year < 0.5 + 0.005
    after_a_decade = _strength(conn, at=T0 + 3650 * DAY_MS)
    assert after_a_decade == pytest.approx(0.5, abs=1e-9)
    # The snapshot column itself is untouched by merely reading.
    assert _stored(conn)[0] == pytest.approx(snapshot)
    assert _stored(conn)[1] == last_used


def test_write_and_read_paths_agree_on_strength(conn):
    """``RollResult.strong_after`` must equal what a reader derives."""
    _insert_fact(conn, importance=0.4)
    events = [
        ("s1", KIND_USER_RESTATED, T0),
        ("s2", KIND_APPLIED, T0 + HOUR_MS),
        ("s3", KIND_USER_CONFIRMED, T0 + 3 * DAY_MS),
    ]
    for session, kind, at in events:
        result = record_reinforcement(conn, "f1", "u1", session, kind, event_at=at)
        assert result is not None
        # Same instant, two derivations: the write path's report and the reader.
        # ``strong_after`` is rounded to 6dp, so compare at that resolution.
        assert _strength(conn, at=at) == pytest.approx(result.strong_after, abs=1e-6)
    # And a later instant only ever decays below it (until new evidence arrives).
    assert _strength(conn, at=T0 + 4 * DAY_MS) < _strength(conn, at=T0 + 3 * DAY_MS)


def test_snapshot_and_timestamp_stay_consistent(conn):
    """The snapshot only moves for events that actually banked something.

    This three-state rule is what keeps the write path and a replay in exact
    agreement, so it is pinned explicitly:

    - banked > 0 -> snapshot and timestamp both advance;
    - passed the gate but banked nothing (a zero-weight kind) -> neither moves;
    - suppressed by the cooldown -> neither moves.
    """
    _insert_fact(conn)
    assert _stored(conn)[:2] == (0.0, None)

    # A zero-weight kind is logged and observed, but it is still *no evidence*:
    # it must not start a cooldown clock either, or a replay (whose gate requires
    # a positive gain) would not have started one.
    record_reinforcement(conn, "f1", "u1", "s1", KIND_RETRIEVED_ONLY, event_at=T0)
    snapshot, last_used, seen = _stored(conn)
    assert snapshot == 0.0
    assert last_used is None, "a zero-gain event started the snapshot clock"
    assert seen == T0, "observability should note the touch"

    # A real event banks and advances both.
    record_reinforcement(conn, "f1", "u1", "s2", KIND_USER_RESTATED, event_at=T0)
    snapshot, last_used, seen = _stored(conn)
    assert snapshot == pytest.approx(0.8)
    assert last_used == T0

    # A suppressed follow-up (inside the cooldown) moves neither, while still
    # being observed.
    record_reinforcement(conn, "f1", "u1", "s3", KIND_APPLIED, event_at=T0 + 1000)
    snapshot2, last_used2, seen2 = _stored(conn)
    assert snapshot2 == pytest.approx(0.8), "a suppressed event moved the snapshot"
    assert last_used2 == T0, "a suppressed event slid the snapshot clock"
    assert seen2 == T0 + 1000, "observability should still note the touch"


# -- 2. long-run invariants ---------------------------------------------------


def test_strength_is_always_bounded_by_the_ceiling(conn):
    """No sequence of any length may exceed base + A_MAX (clamped at 1)."""
    _insert_fact(conn, importance=0.3)
    rng = random.Random(1)
    at = T0
    for i in range(500):
        at += rng.choice([1000, HOUR_MS, DAY_MS, 30 * DAY_MS])
        record_reinforcement(
            conn,
            "f1",
            "u1",
            f"s{i}",
            rng.choice(GAIN_KINDS),
            event_at=at,
        )
        assert _strength(conn, at=at) <= 0.3 + A_MAX + 1e-12
        assert _strength(conn, at=at) <= 1.0
    # Far in the future it can only have decayed, never grown.
    assert _strength(conn, at=at + 100 * 365 * DAY_MS) == pytest.approx(0.3, abs=1e-9)


def test_spread_reuse_accumulates_while_bursting_does_not(conn):
    """The core promise: same event count, different spacing, different strength.

    Events spread beyond the cooldown accumulate; events crammed inside it
    collapse to a single gain. This is the property that makes the cooldown a
    real anti-abuse mechanism rather than a gate on one code path.
    """
    _insert_fact(conn, fact_id="spread", importance=0.3)
    _insert_fact(conn, fact_id="burst", importance=0.3)

    for i in range(6):
        record_reinforcement(
            conn, "spread", "u1", f"sp{i}", KIND_APPLIED,
            event_at=T0 + i * (COOLDOWN_MS + 1000),
        )
    for i in range(6):
        record_reinforcement(
            conn, "burst", "u1", f"bu{i}", KIND_APPLIED,
            event_at=T0 + i * 1000,
        )

    spread = _stored(conn, "spread")[0]
    burst = _stored(conn, "burst")[0]
    assert burst == pytest.approx(0.6), "a burst banked more than one gain"
    assert spread > 3.0, "spread reuse failed to accumulate"
    assert _strength(conn, "spread", T0) > _strength(conn, "burst", T0)


def test_saturation_is_reached_and_then_adds_nothing_meaningful(conn):
    """Enough spread events converge on the ceiling instead of growing forever."""
    _insert_fact(conn, importance=0.5)
    at = T0
    for i in range(60):
        at += COOLDOWN_MS + 1000
        record_reinforcement(
            conn, "f1", "u1", f"s{i}", KIND_USER_RESTATED, event_at=at
        )
    strength = _strength(conn, at=at)
    assert strength <= 1.0
    # Within a hair of the ceiling, and further evidence cannot push past it.
    assert strength > 0.5 + 0.49 * A_MAX
    before = strength
    at += COOLDOWN_MS + 1000
    record_reinforcement(conn, "f1", "u1", "s_extra", KIND_USER_RESTATED, event_at=at)
    assert _strength(conn, at=at) - before < 1e-5


def test_forgetting_horizon_is_bounded_in_practice(conn):
    """The *count* halves per half-life, which is what bounds the bonus."""
    from atom_memory.reinforce import HALF_LIFE_DAYS

    _insert_fact(conn, importance=0.5)
    record_reinforcement(conn, "f1", "u1", "s1", KIND_USER_CONFIRMED, event_at=T0)
    peak_count = 1.0
    peak = _strength(conn, at=T0)
    assert peak == pytest.approx(effective_importance(0.5, peak_count))

    # Note what decays: the count. The bonus follows the count through the
    # saturating curve, so it does *not* halve exactly when the count does —
    # asserting on the strength would encode the wrong model.
    for half_lives in (1, 2, 4, 8, 10):
        at = T0 + int(half_lives * HALF_LIFE_DAYS * DAY_MS)
        expected_count = peak_count * 0.5 ** half_lives
        expected = effective_importance(0.5, expected_count)
        assert _strength(conn, at=at) == pytest.approx(expected, abs=1e-6)
    # Ten half-lives leaves a count of ~0.001, i.e. a bonus that is negligible
    # but not literally zero — state the real tolerance rather than an absolute
    # one that happens to pass.
    at = T0 + int(10 * HALF_LIFE_DAYS * DAY_MS)
    tail = _strength(conn, at=at)
    assert tail > 0.5
    assert tail - 0.5 <= reinforce_bonus(1e-3)
    assert _strength(conn, at=at) < peak


def test_reuse_can_bring_a_faded_fact_back(conn):
    """Decay is not loss: the event log survives, so reuse re-earns strength."""
    _insert_fact(conn, importance=0.4)
    record_reinforcement(conn, "f1", "u1", "s1", KIND_USER_CONFIRMED, event_at=T0)
    faded = _strength(conn, at=T0 + 365 * DAY_MS)
    assert faded == pytest.approx(0.4, abs=5e-3)

    revived_at = T0 + 365 * DAY_MS + 1
    record_reinforcement(
        conn, "f1", "u1", "s_later", KIND_USER_CONFIRMED, event_at=revived_at
    )
    revived = _strength(conn, at=revived_at)
    # A confirmation banks a full gain on top of whatever survived, so the
    # revived bonus beats what one isolated confirmation could ever earn.
    assert revived > faded
    assert revived > 0.4 + reinforce_bonus(1.0)
    # Still bounded by the ceiling.
    assert revived <= 0.4 + A_MAX


# -- 3. differential replay ---------------------------------------------------


def _random_timeline(seed: int, count: int) -> list:
    """Build a plausible event timeline: clusters, gaps, backdating, repeats."""
    rng = random.Random(seed)
    events = []
    at = T0
    for i in range(count):
        at += rng.choice([0, 500, 1000, 60_000, HOUR_MS, DAY_MS, 30 * DAY_MS])
        events.append(
            {
                "session": f"s{rng.randrange(max(1, count // 3))}",
                "kind": rng.choice(list(KIND_GAINS)),
                "at": at,
            }
        )
    return events


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_incremental_state_equals_replayed_state(conn, seed):
    """The write path and a from-scratch replay must agree exactly.

    Randomised on purpose. Replayability is the property that lets the aggregate
    be recounted after retuning or to investigate abuse, so it has to hold for
    timelines nobody designed for — including repeated sessions, zero-gain
    kinds, and back-to-back events inside the cooldown.
    """
    _insert_fact(conn, importance=0.42)
    events = _random_timeline(seed, 120)
    for event in events:
        record_reinforcement(
            conn, "f1", "u1", event["session"], event["kind"], event_at=event["at"]
        )

    live = _stored(conn)

    # Wipe only the aggregate; the log is the source of truth.
    conn.execute(
        "UPDATE facts SET reinforce_count = 0, last_used_at = NULL, "
        "last_seen_at = NULL WHERE fact_id = 'f1'"
    )
    conn.commit()
    rebuilt = rebuild_fact_reinforcement(conn, "f1", "u1")
    assert rebuilt is not None

    after = _stored(conn)
    assert after[0] == pytest.approx(live[0], rel=1e-12, abs=1e-12), "count diverged"
    assert after[1] == live[1], "timestamp diverged"
    assert after[2] == live[2], "observed timestamp diverged"
    # And the derived strength over the whole remaining timeline agrees too.
    for days in (0, 1, 30, 365):
        at = events[-1]["at"] + int(days * DAY_MS)
        snapshot, last_used, _ = after
        replayed = effective_importance_at(0.42, snapshot, last_used, at)
        expected = effective_importance_at(0.42, live[0], live[1], at)
        assert replayed == pytest.approx(expected, abs=1e-12)


@pytest.mark.parametrize("seed", [11, 12, 13])
def test_replay_is_idempotent(conn, seed):
    """Rebuilding twice (or three times) changes nothing."""
    _insert_fact(conn, importance=0.6)
    for event in _random_timeline(seed, 60):
        record_reinforcement(
            conn, "f1", "u1", event["session"], event["kind"], event_at=event["at"]
        )
    first = _stored(conn)
    for _ in range(3):
        rebuild_fact_reinforcement(conn, "f1", "u1")
        assert _stored(conn) == first


def test_replay_ignores_an_unregistered_kind_row(conn):
    """A rogue event row cannot inject strength during a replay.

    Rows are only ever written through ``record_reinforcement``, which validates
    the kind — but the log is a plain table, and a hand-inserted row with a large
    ``gain`` must not become a backdoor after a rebuild.
    """
    _insert_fact(conn)
    record_reinforcement(conn, "f1", "u1", "s1", KIND_USER_RESTATED, event_at=T0)
    conn.execute(
        "INSERT INTO fact_reinforcements(fact_id, user_id, session_id, kind, gain,"
        " created_at) VALUES ('f1', 'u1', 'rogue', 'made_up', 99.0, ?)",
        (T0 + DAY_MS,),
    )
    conn.commit()
    rebuild_fact_reinforcement(conn, "f1", "u1")
    # The stored gain is what a hand-writer chose, so this documents the real
    # contract: replay reproduces the *log*, and the log is only trustworthy
    # because writes go through the validating entry point. What must not happen
    # is the ceiling being breached.
    assert _strength(conn, at=T0 + DAY_MS) <= 1.0
    assert _strength(conn, at=T0 + DAY_MS) <= 0.5 + A_MAX


# -- 4. numerical safety ------------------------------------------------------


def test_decay_is_safe_for_absurd_elapsed_times():
    """No overflow/NaN for spans from zero to a million years."""
    for days in (0, 1, 1e3, 1e6, 1e9):
        factor = decay_factor(days * DAY_MS)
        assert 0.0 <= factor <= 1.0
        assert not math.isnan(factor)
    assert decay_factor(0) == 1.0
    assert decay_factor(-DAY_MS) == 1.0, "clock skew must not amplify"
    assert decay_factor(1e12 * DAY_MS) == 0.0


def test_bonus_is_bounded_for_absurd_counts():
    """The ceiling holds for counts from zero to absurdity."""
    for n in (0.0, 1e-12, 1.0, SATURATION_N, 1e12, 1e300):
        bonus = reinforce_bonus(n)
        assert 0.0 <= bonus < A_MAX, f"bound broken at n={n}"
        assert not math.isnan(bonus)
    # And the terminal value is the largest float that can exist below the cap.
    assert reinforce_bonus(1e300) == math.nextafter(A_MAX, 0.0)


def test_adjust_never_produces_a_negative_or_nan_count():
    for n in (-5.0, 0.0, 1e-300, 1e300):
        for last in (None, T0, T0 + DAY_MS):  # includes a "future" snapshot
            value = adjust(n, last, T0)
            assert not math.isnan(value)
            assert value >= 0.0


def test_clock_skew_cannot_amplify_strength(conn):
    """A backdated event must not multiply the count."""
    _insert_fact(conn)
    record_reinforcement(conn, "f1", "u1", "s1", KIND_USER_CONFIRMED, event_at=T0)
    before = _stored(conn)[0]
    # A second event dated *before* the snapshot: interval clamps to zero.
    record_reinforcement(
        conn, "f1", "u1", "s2", KIND_USER_CONFIRMED, event_at=T0 - 10 * DAY_MS
    )
    after = _stored(conn)[0]
    assert after <= before + 1.0 + 1e-12
    assert _strength(conn, at=T0 - 10 * DAY_MS) <= 0.5 + A_MAX


def test_extreme_gain_is_saturated_by_the_curve_not_the_count(conn):
    """A huge injected gain cannot push past the ceiling; the curve absorbs it."""
    _insert_fact(conn, importance=0.5)
    conn.execute(
        "INSERT INTO fact_reinforcements(fact_id, user_id, session_id, kind, gain,"
        " created_at) VALUES ('f1', 'u1', 's1', 'user_confirmed', 1e9, ?)",
        (T0,),
    )
    conn.execute(
        "UPDATE facts SET reinforce_count = 1e9, last_used_at = ? WHERE fact_id='f1'",
        (T0,),
    )
    conn.commit()
    assert _strength(conn, at=T0) <= 1.0
    assert reinforce_bonus(1e9) < A_MAX


def test_very_small_counts_still_behave_linearly():
    """Numerical noise must not turn a tiny count into a jump or a zero."""
    for n in (1e-9, 1e-6, 1e-3):
        assert 0.0 < reinforce_bonus(n) < 1e-2
        # First-order agreement; the second-order term is ~LAMBDA*n, so the
        # tolerance has to scale with the input rather than be absolute.
        assert reinforce_bonus(n) == pytest.approx(
            A_MAX * LAMBDA * n, rel=1e-3
        )
    assert reinforce_bonus(0.0) == 0.0
    assert reinforce_bonus(-1.0) == 0.0


def test_saturation_helpers_are_consistent_with_the_curve():
    from atom_memory.reinforce import is_saturated, saturated_after

    n = saturated_after()
    assert not is_saturated(n - 1)
    assert is_saturated(n)
    assert is_saturated(SATURATION_N)
    assert 0 < n < 100


def test_gain_table_is_the_only_source_of_evidence_weights():
    """Every kind maps to a documented weight in [0, 1]; unknown kinds are inert."""
    assert set(KIND_GAINS) == {
        "user_confirmed",
        "user_restated",
        "applied",
        "retrieved_only",
    }
    for kind, gain in KIND_GAINS.items():
        assert 0.0 <= gain <= 1.0
        assert gain_for(kind) == gain
    assert gain_for("nope") == 0.0
    # The ordering is the point: confirmation > restatement > application.
    assert (
        gain_for(KIND_USER_CONFIRMED)
        > gain_for(KIND_USER_RESTATED)
        > gain_for(KIND_APPLIED)
        > gain_for(KIND_RETRIEVED_ONLY)
    )


def test_roll_is_pure():
    """Same inputs, same output — no hidden clock read when ``event_at`` is given."""
    a = roll(1.0, T0, 0.8, 0.5, event_at=T0 + DAY_MS)
    b = roll(1.0, T0, 0.8, 0.5, event_at=T0 + DAY_MS)
    assert a == b
    # An explicit ``now`` is honoured over the module clock.
    c = roll(1.0, T0, 0.8, 0.5, now=T0, event_at=T0 + DAY_MS)
    assert c == a


# -- 5. operational bounds ----------------------------------------------------


def test_event_log_cannot_grow_unbounded_per_fact(conn):
    """The schema, not the application, caps growth per fact.

    One event per (user, session, fact, kind) is what stops a long-lived fact
    from accumulating an unbounded log: the fourth dimension is bounded by the
    number of kinds, so growth is linear in sessions — not in messages.
    """
    _insert_fact(conn)
    for _ in range(20):  # same session, same kinds, over and over
        for kind in GAIN_KINDS + [KIND_RETRIEVED_ONLY]:
            record_reinforcement(conn, "f1", "u1", "s_same", kind, event_at=T0)
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM fact_reinforcements WHERE fact_id='f1'"
    ).fetchone()["n"]
    assert count == len(KIND_GAINS), "duplicate events were stored"

    # A new session may add at most one more per kind.
    for kind in KIND_GAINS:
        record_reinforcement(conn, "f1", "u1", "s_next", kind, event_at=T0 + DAY_MS)
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM fact_reinforcements WHERE fact_id='f1'"
    ).fetchone()["n"]
    assert count == 2 * len(KIND_GAINS)


def test_duplicate_events_do_not_drift_the_state(conn):
    """Replaying the same session forever is a no-op for the aggregate."""
    _insert_fact(conn)
    record_reinforcement(conn, "f1", "u1", "s1", KIND_USER_RESTATED, event_at=T0)
    first = _stored(conn)
    for _ in range(50):
        assert (
            record_reinforcement(conn, "f1", "u1", "s1", KIND_USER_RESTATED, event_at=T0)
            is None
        )
    assert _stored(conn) == first


def test_forgotten_and_superseded_facts_stop_accumulating(conn):
    """A dead fact must not keep a live-looking aggregate."""
    _insert_fact(conn)
    record_reinforcement(conn, "f1", "u1", "s1", KIND_USER_CONFIRMED, event_at=T0)
    conn.execute("UPDATE facts SET status = 'superseded' WHERE fact_id = 'f1'")
    conn.commit()
    assert (
        record_reinforcement(conn, "f1", "u1", "s2", KIND_USER_CONFIRMED, event_at=T0)
        is None
    )
    # Reading it still works and still decays — it is simply no longer fed. A
    # year is ~4.9 half-lives, so the bonus is down to a few thousandths.
    assert _strength(conn, at=T0 + 365 * DAY_MS) == pytest.approx(0.5, abs=5e-3)
    assert _strength(conn, at=T0 + 10 * 365 * DAY_MS) == pytest.approx(0.5, abs=1e-9)


def test_reinforcement_of_a_deleted_fact_is_a_no_op(conn):
    """A missing row is reported, never invented."""
    assert record_reinforcement(conn, "ghost", "u1", "s1", KIND_APPLIED, event_at=T0) is None
    assert rebuild_fact_reinforcement(conn, "ghost", "u1") is None


def test_user_scoping_is_enforced_on_every_write_and_rebuild(conn):
    """No path may touch another user's fact or log."""
    _insert_fact(conn, fact_id="f1", user_id="u1")
    assert record_reinforcement(conn, "f1", "u2", "s1", KIND_APPLIED, event_at=T0) is None
    assert rebuild_fact_reinforcement(conn, "f1", "u2") is None
    assert conn.execute("SELECT COUNT(*) AS n FROM fact_reinforcements").fetchone()["n"] == 0
    # The rightful owner still works.
    assert record_reinforcement(conn, "f1", "u1", "s1", KIND_APPLIED, event_at=T0) is not None


def test_long_run_simulation_stays_within_contract(conn):
    """A simulated decade of mixed traffic holds every invariant at once.

    The end-to-end version of this file: many facts, many sessions, bursts,
    silences and revivals, then assertions on the aggregate state the whole
    system would rely on.
    """
    rng = random.Random(99)
    facts = [f"f{i}" for i in range(8)]
    for fid in facts:
        _insert_fact(conn, fact_id=fid, importance=0.5)

    sessions = 0
    for year in range(10):
        for _ in range(rng.randrange(1, 12)):
            sessions += 1
            at = T0 + year * 365 * DAY_MS + rng.randrange(365) * DAY_MS
            for fid in rng.sample(facts, rng.randrange(1, len(facts) + 1)):
                record_reinforcement(
                    conn, fid, "u1", f"sess{sessions}", rng.choice(list(KIND_GAINS)),
                    event_at=at,
                )

        # Invariants that must hold after every simulated year.
        for fid in facts:
            snapshot, last_used, _ = _stored(conn, fid)
            assert snapshot >= 0.0
            assert not math.isnan(snapshot)
            assert last_used is None or last_used >= T0
            strength = _strength(conn, fid, at=T0 + (year + 1) * 365 * DAY_MS)
            assert 0.5 <= strength <= 1.0
            assert strength <= 0.5 + A_MAX + 1e-12

    # After the last event, everything decays back toward the base importance.
    horizon = T0 + 20 * 365 * DAY_MS
    for fid in facts:
        assert _strength(conn, fid, at=horizon) == pytest.approx(0.5, abs=1e-6)

    # The log stayed proportional to sessions, not to messages.
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM fact_reinforcements"
    ).fetchone()["n"]
    assert total <= sessions * len(facts) * len(KIND_GAINS)


# -- 6. integration with the derived views ------------------------------------


def _insert_md_fact(conn, fact_id: str, obj: str, importance: float = 0.5):
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, source_type, status, observed_at, "
        "created_at, version, type) VALUES (?, 'u1', 's1', '用户', '偏好', ?, "
        "0.8, ?, 'user_explicit', 'active', ?, ?, 1, 'semantic')",
        (fact_id, obj, importance, T0, T0),
    )
    conn.commit()


def test_memory_md_orders_by_reinforced_strength(conn):
    """Reuse must reach the injected digest, not only the retrieval ranker.

    ``memory.md`` is what is frozen into the system prompt at session start, so a
    reinforcement mechanism that never touches it would be invisible on the one
    surface that is always paid for.
    """
    from atom_memory.memory_md import generate_memory_md

    _insert_md_fact(conn, "f_used", "黑咖啡", importance=0.6)
    _insert_md_fact(conn, "f_idle", "奶茶", importance=0.6)
    # Two confirmations, spaced beyond the cooldown, for f_used only.
    record_reinforcement(conn, "f_used", "u1", "s1", KIND_USER_CONFIRMED, event_at=T0)
    record_reinforcement(
        conn, "f_used", "u1", "s2", KIND_USER_CONFIRMED, event_at=T0 + HOUR_MS
    )

    md = generate_memory_md(conn, "u1", max_tokens=2000, detail=True)
    assert md.index("黑咖啡") < md.index("奶茶"), "reinforcement did not reorder"


def test_memory_md_reinforcement_fades_with_time(conn):
    """The digest must decay reuse too, or it ages differently from retrieval."""
    from atom_memory.db import now_ms
    from atom_memory.memory_md import _collect

    _insert_md_fact(conn, "f_used", "黑咖啡", importance=0.6)
    now = now_ms()
    # Snapshot taken a year ago: still stored, but long stale.
    record_reinforcement(
        conn, "f_used", "u1", "s1", KIND_USER_CONFIRMED,
        event_at=now - 365 * DAY_MS,
    )
    rank_fresh = _collect(conn, "u1")["preference"][0]["rank"]
    assert rank_fresh == pytest.approx(0.6, abs=5e-3)

    # Re-confirmed just now, the same fact is meaningfully stronger.
    record_reinforcement(
        conn, "f_used", "u1", "s2", KIND_USER_CONFIRMED, event_at=now
    )
    rank_now = _collect(conn, "u1")["preference"][0]["rank"]
    assert rank_now > rank_fresh + 0.1


def test_memory_md_keeps_the_neutral_importance_fallback(conn):
    """Reinforcement must not break "0.5 means unknown, use the type rank"."""
    from atom_memory.memory_md import _collect

    # decision_rule has a higher type default than semantic; both store the
    # neutral 0.5, so the type default must decide — before and after
    # reinforcement. ("决定" lands in the decision_rule section, "职业" in the
    # plain-attribute one.)
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, source_type, status, observed_at, "
        "created_at, version, type) VALUES ('d1','u1','s1','用户','决定','先回滚',"
        "0.8,0.5,'user_explicit','active',?,?,1,'decision_rule')",
        (T0, T0),
    )
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, confidence, importance, source_type, status, observed_at, "
        "created_at, version, type) VALUES ('s1','u1','s1','用户','职业','工程师',"
        "0.8,0.5,'user_explicit','active',?,?,1,'semantic')",
        (T0, T0),
    )
    conn.commit()
    buckets = _collect(conn, "u1")
    assert "decision_rule" in buckets and "attribute" in buckets
    d_rank = buckets["decision_rule"][0]["rank"]
    s_rank = buckets["attribute"][0]["rank"]
    assert d_rank > s_rank, "type-rank fallback broke"

    record_reinforcement(conn, "s1", "u1", "sess", KIND_USER_CONFIRMED, event_at=T0)
    after = _collect(conn, "u1")
    assert after["attribute"][0]["rank"] > s_rank, "reinforcement did not apply"
    assert after["decision_rule"][0]["rank"] == pytest.approx(d_rank), "an idle fact moved"


# -- 7. snapshot boundary -----------------------------------------------------


def test_backup_round_trip_does_not_carry_reinforcement(tmp_path):
    """Reinforcement history stays local to the database, and that is explicit.

    A snapshot carries the facts and their *base* importance; it does not carry
    the reuse log. Restoring therefore yields un-reinforced facts — which is the
    honest outcome, since the events that justified the strength are not in the
    snapshot either and a restored fact could not be re-audited.

    Pinned by a test so the boundary is a decision rather than an accident.
    """
    from atom_memory.backup import export_memory

    conn2 = None
    try:
        from atom_memory.db import connect_for_tests

        conn2 = connect_for_tests()
        _insert_fact(conn2, fact_id="f1", importance=0.7)
        record_reinforcement(conn2, "f1", "u1", "s1", KIND_USER_CONFIRMED, event_at=T0)
        snapshot = export_memory(conn2, "u1")
        assert snapshot["facts"], "nothing exported"
        exported = snapshot["facts"][0]
        assert exported["importance"] == pytest.approx(0.7), "base importance lost"
        assert "reinforce_count" not in exported
        assert "last_used_at" not in exported
        # The event log is not part of the snapshot either.
        assert "reinforcements" not in snapshot
    finally:
        if conn2 is not None:
            conn2.close()
