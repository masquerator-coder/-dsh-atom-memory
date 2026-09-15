"""Tests for the validation chain (validator.py, spec order:
empty -> degenerate -> confidence -> idempotency -> conflict -> privacy)."""

from __future__ import annotations

from atom_memory.db import connect_for_tests
from atom_memory.models import FactCandidate
from atom_memory.validator import _predicate_core, validate


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
        # The duplicate is a *candidate*, not a fact: `suppressed` (whose
        # contract is "the existing fact_id this candidate represents") must be
        # left unset so the worker never tries to reinforce a candidate id as a
        # fact_id (see worker._process_extract).
        assert r.suppressed is None
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


def test_idempotency_key_resolves_applied_fact_for_reinforcement():
    """An idempotent duplicate whose claim was already applied to an active
    fact reports the real ``fact_id`` in ``suppressed``, so the worker's
    reuse-reinforcement can fire (restating a stored claim is the cleanest
    reuse evidence)."""
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, status, observed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', 1, 1)",
            ("f_applied", "u1", "s1", "用户", "偏好", "黑咖啡",),
        )
        conn.execute(
            "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
            "turn_id, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("c_existing", "u1", "s1", 0, "key-abc", 1),
        )
        conn.commit()
        c = make(candidate_id="c_new", idempotency_key="key-abc", object="黑咖啡")
        r = validate(c, conn=conn)
        assert not r.ok and r.kind == "idempotent"
        assert r.suppressed == "f_applied", "must carry the real fact_id, not a candidate id"
    finally:
        conn.close()


def test_idempotency_key_with_no_fact_stays_pure_suppress():
    """When the prior candidate never became an active fact, `suppressed` is
    left unset (nothing to reinforce) — never a candidate id masquerading as a
    fact id."""
    conn = connect_for_tests()
    try:
        conn.execute(
            "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
            "turn_id, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("c_existing", "u1", "s1", 0, "key-abc", 1),
        )
        conn.commit()
        c = make(candidate_id="c_new", idempotency_key="key-abc", object="黑咖啡")
        r = validate(c, conn=conn)
        assert not r.ok and r.kind == "idempotent"
        assert r.suppressed is None
    finally:
        conn.close()


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


# ---- knowledge facts are multi-valued under one predicate ------------------------

def _insert_typed(conn, fact_id, predicate, obj, ftype):
    conn.execute(
        "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
        "object, type, status, observed_at, created_at) "
        "VALUES (?, 'u1', 's1', '用户', ?, ?, ?, 'active', 1, 1)",
        (fact_id, predicate, obj, ftype),
    )


def test_second_sop_with_same_predicate_does_not_conflict():
    """Two SOPs legitimately share a predicate: the second is a new item."""
    conn = connect_for_tests()
    try:
        _insert_typed(conn, "f1", "发布SOP", "A方案", "sop")
        conn.commit()
        r = validate(make(candidate_id="c1", predicate="发布SOP", object="B方案", type="sop"), conn=conn)
        assert r.ok, r.reason
    finally:
        conn.close()


def test_second_lesson_with_same_predicate_does_not_conflict():
    """Light knowledge is independent too."""
    conn = connect_for_tests()
    try:
        _insert_typed(conn, "f1", "教训", "先备份", "lesson")
        conn.commit()
        r = validate(make(candidate_id="c1", predicate="教训", object="先灰度", type="lesson"), conn=conn)
        assert r.ok, r.reason
    finally:
        conn.close()


def test_identical_knowledge_object_is_still_suppressed():
    """Multi-valued does not disable dedup: the same item is still idempotent."""
    conn = connect_for_tests()
    try:
        _insert_typed(conn, "f1", "发布SOP", "A方案", "sop")
        conn.commit()
        r = validate(make(candidate_id="c1", predicate="发布SOP", object="A方案", type="sop"), conn=conn)
        assert not r.ok and r.kind == "idempotent"
    finally:
        conn.close()


def test_semantic_attribute_stays_single_valued():
    """The knowledge exemption must not loosen plain attributes."""
    conn = connect_for_tests()
    try:
        _insert_typed(conn, "f1", "职业", "工程师", "semantic")
        conn.commit()
        r = validate(make(candidate_id="c1", predicate="职业", object="医生", type="semantic"), conn=conn)
        assert not r.ok and r.kind == "conflict"
    finally:
        conn.close()


# ---- degenerate: placeholder / predicate-echo objects ---------------------------
#
# Real-world junk observed in the live store: describing the memory feature
# itself produced facts such as 「起到的作用: 起到的作用」 and
# 「被谁调用: 被调用的对象」, which polluted the summary and memory.md.

def test_object_echoing_predicate_fails():
    """Rule A: the object merely repeats the predicate."""
    r = validate(make(candidate_id="c1", subject="summary（摘要）",
                      predicate="起到的作用", object="起到的作用"))
    assert not r.ok and r.kind == "degenerate"


def test_object_echoing_subject_fails():
    """Rule A also covers an object that repeats the subject."""
    r = validate(make(candidate_id="c1", subject="用户", predicate="说明", object="用户"))
    assert not r.ok and r.kind == "degenerate"


def test_placeholder_object_fails():
    """Rule B: a known placeholder phrase is rejected outright."""
    for placeholder in ("待定", "未知", "其他", "对象"):
        r = validate(make(candidate_id="c1", predicate="部署时间", object=placeholder))
        assert not r.ok and r.kind == "degenerate", placeholder


def test_predicate_core_echo_fails():
    """Rule C: object reuses the predicate core and ends in a generic head."""
    r = validate(make(candidate_id="c1", subject="summary（摘要）",
                      predicate="被谁调用", object="被调用的对象"))
    assert not r.ok and r.kind == "degenerate"


def test_descriptive_object_is_not_flagged():
    """Rule C's length guard keeps informative objects alive."""
    r = validate(make(candidate_id="c1", subject="用户", predicate="角色",
                      object="项目经理的角色"))
    assert r.ok, r.reason


def test_path_like_object_is_not_flagged():
    """Ordinary values (paths, names) must survive the new check."""
    r = validate(make(candidate_id="c1", subject="用户", predicate="obsidian笔记位置",
                      object=r"C:\Users\fuqia\Documents\OSpace"))
    assert r.ok, r.reason
    r2 = validate(make(candidate_id="c2", subject="用户", predicate="名字", object="小强哥"))
    assert r2.ok, r2.reason


def test_degenerate_reported_before_confidence():
    """Ordering: the content check runs before the numeric one."""
    r = validate(make(candidate_id="c1", predicate="部署时间", object="待定", confidence=2.0))
    assert r.kind == "degenerate"


def test_predicate_core_drops_longer_question_words_first():
    """'哪里' must not be shortened to a stray '里' by the shorter '哪'."""
    core = _predicate_core("数据在哪里")
    assert "里" not in core
    assert "数据" in core

