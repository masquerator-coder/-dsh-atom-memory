"""Tests for the rule-based extractor (extractor.py, spec 8.1)."""

from __future__ import annotations

import json

from atom_memory.extractor import Extractor, extract_rules


def _first(text):
    out = extract_rules(text, "u1", "s1", turn_id=1)
    assert len(out) == 1, f"expected 1 candidate for {text!r}, got {len(out)}"
    return out[0]


# ---- positive preference pattern: (我|用户)(喜欢|偏好|爱|习惯)X ----

def test_positive_like():
    c = _first("用户喜欢黑咖啡")
    assert (c.subject, c.predicate, c.object) == ("用户", "偏好", "黑咖啡")
    assert c.user_id == "u1" and c.session_id == "s1" and c.turn_id == 1
    assert c.qualifiers is None  # no negation


def test_positive_first_person():
    c = _first("我喜欢跑步")
    assert (c.subject, c.predicate, c.object) == ("用户", "偏好", "跑步")


def test_positive_preference_word():
    c = _first("用户偏好香草拿铁")
    assert (c.subject, c.predicate, c.object) == ("用户", "偏好", "香草拿铁")


def test_positive_habit():
    c = _first("用户习惯早睡")
    assert (c.subject, c.predicate, c.object) == ("用户", "偏好", "早睡")


def test_positive_love():
    c = _first("我爱编程")
    assert (c.subject, c.predicate, c.object) == ("用户", "偏好", "编程")


# ---- negative preference pattern: (我|用户)(不喜欢|讨厌|厌恶)X ----

def test_negative_dislike():
    c = _first("用户不喜欢加糖")
    assert (c.subject, c.predicate, c.object) == ("用户", "偏好", "加糖")
    q = json.loads(c.qualifiers)
    assert q.get("negation") is True


def test_negative_hate():
    c = _first("我讨厌香菜")
    q = json.loads(c.qualifiers)
    assert q.get("negation") is True


def test_negative_loathe():
    c = _first("用户厌恶吵闹环境")
    q = json.loads(c.qualifiers)
    assert q.get("negation") is True


# ---- attribute pattern: (我|用户)的X是Y ----

def test_attribute():
    c = _first("用户的职业是工程师")
    assert (c.subject, c.predicate, c.object) == ("用户", "职业", "工程师")


def test_attribute_first_person():
    c = _first("我的家乡是成都")
    assert (c.subject, c.predicate, c.object) == ("用户", "家乡", "成都")


# ---- no-match / edge cases ----

def test_no_match_returns_empty():
    assert extract_rules("今天天气不错", "u1", "s1") == []


def test_blank_text_returns_empty():
    assert extract_rules("   ", "u1", "s1") == []


def test_empty_string_returns_empty():
    assert extract_rules("", "u1", "s1") == []


def test_extractor_class_no_llm():
    ex = Extractor()
    out = ex.extract("用户喜欢黑咖啡", "u1", "s1", turn_id=2)
    assert len(out) == 1
    assert out[0].turn_id == 2


def test_extractor_class_with_llm():
    calls = {}

    def fake_llm(text, user_id, session_id, turn_id):
        calls["text"] = text
        return [{"subject": "用户", "predicate": "职业", "object": "数据科学家"}]

    ex = Extractor(llm_extractor=fake_llm)
    out = ex.extract("用户喜欢黑咖啡", "u1", "s1", turn_id=3)
    # LLM-first: the LLM candidate is authoritative; rules are suppressed.
    assert len(out) == 1
    assert calls["text"] == "用户喜欢黑咖啡"
    assert (out[0].subject, out[0].predicate, out[0].object) == (
        "用户",
        "职业",
        "数据科学家",
    )


def test_extractor_llm_failure_is_silent(monkeypatch):
    """A throwing LLM extractor must fall back to rule extraction."""

    def bad_llm(*args, **kwargs):
        raise RuntimeError("llm down")

    ex = Extractor(llm_extractor=bad_llm)
    out = ex.extract("用户喜欢黑咖啡", "u1", "s1")
    # LLM threw -> rule fallback produces the preference candidate.
    assert len(out) == 1
    assert (out[0].predicate, out[0].object) == ("偏好", "黑咖啡")


def test_extractor_llm_empty_falls_back_to_rules():
    """An LLM that returns no candidates falls back to the rule engine."""
    ex = Extractor(llm_extractor=lambda *a, **k: [])
    out = ex.extract("用户喜欢黑咖啡", "u1", "s1")
    assert len(out) == 1
    assert (out[0].predicate, out[0].object) == ("偏好", "黑咖啡")


def test_extractor_llm_none_falls_back_to_rules():
    """An LLM that returns None falls back to the rule engine."""
    ex = Extractor(llm_extractor=lambda *a, **k: None)
    out = ex.extract("用户喜欢黑咖啡", "u1", "s1")
    assert len(out) == 1
    assert out[0].object == "黑咖啡"


def test_extractor_llm_first_wins_over_rules():
    """When both could produce candidates, the LLM result is the only one used."""
    ex = Extractor(llm_extractor=lambda *a, **k: [{"subject": "用户", "predicate": "项目", "object": "发布"}])
    out = ex.extract("用户喜欢黑咖啡", "u1", "s1")
    # Even though the text matches the 偏好 rule, the LLM result wins alone.
    assert len(out) == 1
    assert (out[0].predicate, out[0].object) == ("项目", "发布")


def test_extractor_llm_normalizes_single_dict_and_candidate():
    """LLM output may be a single dict or a FactCandidate."""
    from atom_memory.models import FactCandidate

    ex = Extractor(llm_extractor=lambda *a, **k: {"subject": "用户", "predicate": "职业", "object": "工程师"})
    out = ex.extract("随便说点什么", "u1", "s1")
    assert len(out) == 1 and out[0].object == "工程师"

    cand = FactCandidate(
        candidate_id="c-llm",
        user_id="u1",
        session_id="s1",
        subject="用户",
        predicate="偏好",
        object="跑步",
    )
    ex2 = Extractor(llm_extractor=lambda *a, **k: cand)
    out2 = ex2.extract("随便说点什么", "u1", "s1")
    assert len(out2) == 1 and out2[0].object == "跑步"


def test_llm_dict_candidate_inherits_context_scope():
    """A dict candidate lacking owner fields inherits the call's user/session."""
    ex = Extractor(
        llm_extractor=lambda *a, **k: [{"subject": "用户", "predicate": "事件", "object": "发布"}]
    )
    out = ex.extract("随便说点什么", "u1", "s1", turn_id=5)
    assert len(out) == 1
    assert out[0].user_id == "u1"
    assert out[0].session_id == "s1"
    assert out[0].turn_id == 5


def test_extractor_no_llm_configured_uses_rules():
    """With no LLM extractor the rule engine is the only path."""
    ex = Extractor()
    out = ex.extract("用户喜欢黑咖啡", "u1", "s1")
    assert len(out) == 1
    assert (out[0].predicate, out[0].object) == ("偏好", "黑咖啡")


# ---- procedural workflow pattern ------------------------------------------------

def test_procedural_workflow_with_steps():
    c = _first("发布流程是1.构建 2.测试 3.部署")
    assert c.type == "procedural"
    assert c.predicate == "发布流程"
    assert c.object == "构建 -> 测试 -> 部署"
    q = json.loads(c.qualifiers)
    assert q["steps"] == ["构建", "测试", "部署"]


def test_procedural_first_person_name_cleanup():
    c = _first("我的发布流程是1.构建 2.测试 3.部署")
    assert c.type == "procedural"
    assert c.predicate == "发布流程"  # leading 我的 stripped
    q = json.loads(c.qualifiers)
    assert q["steps"] == ["构建", "测试", "部署"]


def test_procedural_experience_synonym():
    c = _first("我做咖啡的经验是1.磨豆 2.萃取 3.打奶")
    assert c.type == "procedural"
    assert "做咖啡" in c.predicate
    q = json.loads(c.qualifiers)
    assert q["steps"] == ["磨豆", "萃取", "打奶"]


def test_procedural_ordering_words():
    c = _first("发布流程是首先提交然后合并最后部署")
    assert c.type == "procedural"
    q = json.loads(c.qualifiers)
    # ordering words are stripped from steps
    assert q["steps"] == ["提交", "合并", "部署"]


# ---- episodic event pattern --------------------------------------------------------

def test_episodic_with_time():
    c = _first("今天完成了项目发布")
    assert c.type == "episodic"
    assert c.predicate == "事件"
    assert c.object == "项目发布"
    q = json.loads(c.qualifiers)
    assert q.get("episodic") is True
    assert q.get("when") == "今天"


def test_episodic_first_person_past():
    c = _first("昨天我遇到了一个线上事故")
    assert c.type == "episodic"
    assert c.predicate == "事件"
    assert c.object == "一个线上事故"
    q = json.loads(c.qualifiers)
    assert q.get("when") == "昨天"


def test_episodic_without_time_still_classified():
    c = _first("我完成了项目发布")
    assert c.type == "episodic"
    assert c.predicate == "事件"
    q = json.loads(c.qualifiers)
    assert q.get("episodic") is True
    assert q.get("when") in (None, "")


def test_time_only_sentence_returns_empty():
    # a time word alone (no event verb) must not be a false positive
    assert extract_rules("今天天气不错", "u1", "s1") == []


# ---- knowledge categories: lesson / SOP / decision_rule -----------------------------

def test_lesson_extracts_content():
    """这次的教训是X -> lesson fact with rule predicate and full content body."""
    c = _first("这次的教训是不能在没测试的情况下直接上线")
    assert c.type == "lesson"
    assert c.predicate == "教训"
    # object is the short headline; content carries the full lesson body
    assert c.object == "不能在没测试的情况下直接上线"
    assert c.content == "不能在没测试的情况下直接上线"
    q = json.loads(c.qualifiers)
    assert q.get("knowledge") is True


def test_lesson_takeaway_synonym():
    c = _first("我的心得是发布前先写变更说明")
    assert c.type == "lesson"
    assert c.predicate == "心得"
    assert c.content == "发布前先写变更说明"


def test_lesson_beats_procedural_precedence():
    """经验 (overlaps the procedural synonym set) still classifies as lesson
    when it appears in lesson form (我的/用户的...的经验是)."""
    c = _first("我的经验是先做完整备份再升级")
    assert c.type == "lesson"
    assert c.predicate == "经验"
    assert c.content == "先做完整备份再升级"


def test_sop_extracts_content():
    """发布SOP是... -> sop fact with named predicate and full content body."""
    c = _first("发布SOP是先构建再测试最后部署")
    assert c.type == "sop"
    assert c.predicate == "发布"  # the workflow name before the SOP marker
    q = json.loads(c.qualifiers)
    assert q.get("knowledge") is True
    assert c.content == "先构建再测试最后部署"


def test_sop_standard_flow():
    """上线的标准流程是... -> sop, not a plain procedural workflow."""
    c = _first("上线的标准流程是评审、测试、灰度上线")
    assert c.type == "sop"
    assert c.content is not None and "评审" in c.content


def test_decision_rule_extracts_content():
    """当X时应该Y -> decision_rule fact with full rule body in content."""
    c = _first("当线上出事故时应该先回滚再排查")
    assert c.type == "decision_rule"
    assert c.predicate == "决策规则"
    q = json.loads(c.qualifiers)
    assert q.get("knowledge") is True
    assert c.content is not None
    assert "线上出事故" in c.content and "回滚" in c.content


def test_decision_rule_negated_action():
    c = _first("当发布失败时不要直接重试")
    assert c.type == "decision_rule"
    assert c.content is not None and "发布失败" in c.content and "不要" in c.content


def test_sop_needs_explicit_flow_keyword():
    """A generic attribute sentence must NOT be misclassified as an SOP."""
    c = _first("用户的职业是工程师")
    assert c.type != "sop"


# ---- LLM-first content passthrough ---------------------------------------------------

def test_llm_candidate_passthrough_keeps_type_and_content():
    """An LLM dict candidate's type + content are preserved through the pipeline."""
    ex = Extractor(
        llm_extractor=lambda *a, **k: [
            {
                "subject": "用户",
                "predicate": "教训",
                "object": "先备份再升级",
                "type": "lesson",
                "content": "升级任何生产依赖前先做完整备份",
            }
        ]
    )
    out = ex.extract("随便说点什么", "u1", "s1")
    assert len(out) == 1
    assert out[0].type == "lesson"
    assert out[0].content == "升级任何生产依赖前先做完整备份"
    assert out[0].object == "先备份再升级"
    # dict candidate inherits scope
    assert out[0].user_id == "u1" and out[0].session_id == "s1"

