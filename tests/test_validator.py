"""Tests for the validation chain (validator.py, spec order:
empty -> confidence -> idempotency -> conflict -> privacy)."""

from __future__ import annotations

from dsh_atom_memory.db import connect_for_tests
from dsh_atom_memory.models import FactCandidate
from dsh_atom_memory.validator import validate


def make(**overrides) -> FactCandidate:
    base = dict(
        candidate_id="c1",
        user_id="u1",
        session_id="s1",
        turn_id=0,
        subject="用户",
        predicate="偏好",
        object="黑咖啡",
        confidence=0.7,
        importance=0.6,
        privacy="private",
    )
    base.update(overrides)
    return FactCandidate(**base)


# ---- empty ---------------------------------------------------------------

def test_empty_subject_fails():
    r = validate(make(subject="", object="黑咖啡"))
    assert not r.ok and r.kind == "empty"


def test_empty_predicate_fails():
    r = validate(make(predicate=None))
    assert not r.ok and r.kind == "empty"


def test_empty_object_fails():
    r = validate(make(object="   "))
    assert not r.ok and r.kind == "empty"


# ---- confidence -----------------------------------------------------------

def test_confidence_above_range_fails():
    r = validate(make(confidence=1.5))
    assert not r.ok and r.kind == "confidence"


def test_confidence_below_range_fails():
    r = validate(make(confidence=-0.1))
    assert not r.ok and r.kind == "confidence"


def test_confidence_boundary_ok():
    r = validate(make(confidence=1.0))
    assert r.ok


# ---- ordering: empty beats confidence ----------------------------------------

def test_empty_reported_before_confidence():
    # empty subject AND out-of-range confidence -> must report 'empty'
    r = validate(make(subject="", confidence=2.0))
    assert r.kind == "empty"


# ---- idempotency ------------------------------------------------------------

def test_idempotency_suppresses_duplicate_key():
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
            "turn_id, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("c_existing", "u1", "s1", 0, "key-abc", 1),
        )
        conn.commit()
        c = make(candidate_id="c_new", idempotency_key="key-abc")
        r = validate(c, conn=conn)
        assert not r.ok and r.kind == "idempotent"
        assert r.suppressed == "c_existing"
    finally:
        conn.close()


def test_new_idempotency_key_passes():
    conn = connect_for_tests()
    try:
        c = make(idempotency_key="key-xyz")
        r = validate(c, conn=conn)
        assert r.ok
    finally:
        conn.close()


def test_no_key_passes_idempotency():
    r = validate(make(idempotency_key=None))
    assert r.ok


# ---- conflict ---------------------------------------------------------------
#
# 偏好 (and other multi-valued predicates) accept many objects concurrently;
# single-valued attributes (职业, 家乡, ...) only allow one active object.

def test_conflicting_attribute_detected():
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, status, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', 1, 1)",
            ("f1", "u1", "s1", "用户", "职业", "工程师",),
        )
        conn.commit()
        # same single-valued predicate, different object -> conflict
        r = validate(make(candidate_id="c1", predicate="职业", object="教师"), conn=conn)
        assert not r.ok and r.kind == "conflict"
        assert r.conflict_with == "f1"
    finally:
        conn.close()


def test_independent_preferences_do_not_conflict():
    """Different objects under a multi-valued predicate are not conflicts."""
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, status, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', 1, 1)",
            ("f1", "u1", "s1", "用户", "偏好", "黑咖啡",),
        )
        conn.commit()
        # 偏好 is multi-valued: liking sugar alongside black coffee is fine.
        r = validate(make(object="加糖"), conn=conn)
        assert r.ok
    finally:
        conn.close()


def test_negation_contradiction_detected():
    """Same object with flipped negation is a direct contradiction."""
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, status, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', 1, 1)",
            ("f1", "u1", "s1", "用户", "偏好", "加糖",),
        )
        conn.commit()
        # negative preference for the same object the user already likes
        cand = make(candidate_id="c1", object="加糖")
        cand.qualifiers = '{"negation": true}'
        r = validate(cand, conn=conn)
        assert not r.ok and r.kind == "conflict"
    finally:
        conn.close()


def test_identical_active_fact_is_suppressed():
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, status, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', 1, 1)",
            ("f1", "u1", "s1", "用户", "偏好", "黑咖啡",),
        )
        conn.commit()
        r = validate(make(object="黑咖啡"), conn=conn)
        assert not r.ok and r.kind == "idempotent"  # duplicate reinforcement
        assert r.suppressed == "f1"
    finally:
        conn.close()


def test_conflict_ignores_inactive_facts():
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, status, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'retracted', 1, 1)",
            ("f1", "u1", "s1", "用户", "偏好", "黑咖啡",),
        )
        conn.commit()
        r = validate(make(object="奶茶"), conn=conn)
        assert r.ok  # retracted fact does not conflict
    finally:
        conn.close()


def test_user_isolation_in_conflict():
    """A conflicting fact belonging to another user must not count."""
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, status, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', 1, 1)",
            ("f1", "u_OTHER", "s1", "用户", "偏好", "黑咖啡",),
        )
        conn.commit()
        r = validate(make(object="奶茶"), conn=conn)
        assert r.ok  # different user -> no conflict
    finally:
        conn.close()


# ---- ordering: conflict beats privacy ---------------------------------------

def test_conflict_reported_before_privacy():
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, status, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', 1, 1)",
            ("f1", "u1", "s1", "用户", "职业", "工程师",),
        )
        conn.commit()
        # conflict (single-valued attribute) must be reported before privacy
        r = validate(make(predicate="职业", object="教师", privacy="public"), conn=conn)
        assert r.kind == "conflict"
    finally:
        conn.close()


# ---- episodic: many independent events do not conflict --------------------------

def test_different_episodic_events_do_not_conflict():
    """Two different events (predicate 事件) are independent, not a conflict."""
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, type, status, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'episodic', 'active', 1, 1)",
            ("f1", "u1", "s1", "用户", "事件", "项目发布",),
        )
        conn.commit()
        # a second, different event is fine (episodic -> independent)
        cand = make(candidate_id="c1", predicate="事件", object="故障复盘", type="episodic")
        r = validate(cand, conn=conn)
        assert r.ok
    finally:
        conn.close()


def test_episodic_type_alone_marks_independent():
    """Even with an arbitrary predicate, type=episodic avoids conflicts."""
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, type, status, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'episodic', 'active', 1, 1)",
            ("f1", "u1", "s1", "用户", "经历", "跑了一次全马",),
        )
        conn.commit()
        cand = make(candidate_id="c1", predicate="经历", object="登了一次山", type="episodic")
        r = validate(cand, conn=conn)
        assert r.ok
    finally:
        conn.close()


# ---- procedural: one canonical workflow per name --------------------------------

def test_same_workflow_different_steps_conflicts():
    """A workflow name is single-valued: a new step list conflicts (goes to replace)."""
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, type, status, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'procedural', 'active', 1, 1)",
            ("f1", "u1", "s1", "用户", "发布流程", "构建 -> 测试 -> 部署",),
        )
        conn.commit()
        cand = make(
            candidate_id="c1", predicate="发布流程",
            object="构建 -> 测试", type="procedural",
        )
        r = validate(cand, conn=conn)
        assert not r.ok and r.kind == "conflict"
        assert r.conflict_with == "f1"
    finally:
        conn.close()


# ---- privacy ----------------------------------------------------------------

def test_privacy_mismatch_fails():
    r = validate(make(privacy="public"), privacy_filter="private")
    assert not r.ok and r.kind == "privacy"


def test_blank_privacy_filled_from_filter():
    c = make(privacy="")
    r = validate(c, privacy_filter="private")
    assert r.ok
    assert c.privacy == "private"


# ---- happy path -------------------------------------------------------------

def test_valid_candidate_passes():
    r = validate(make())
    assert r.ok and r.kind == "ok"
