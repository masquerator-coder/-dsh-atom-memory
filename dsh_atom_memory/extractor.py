"""Rule-based atomic fact extraction.

The extractor turns user utterances into structured
:class:`~dsh_atom_memory.models.FactCandidate` objects by matching a small
set of hand-written Chinese pattern rules. An optional LLM extractor
(``MemConfig.llm_extractor``) can be layered on top to surface additional
candidates that the rules miss.
"""

from __future__ import annotations

import logging
import re
from typing import Callable, List, Optional

from .models import (
    FactCandidate,
    PRED_EVENT,
    TYPE_DECISION_RULE,
    TYPE_EPISODIC,
    TYPE_LESSON,
    TYPE_PROCEDURAL,
    TYPE_SEMANTIC,
    TYPE_SOP,
)

logger = logging.getLogger(__name__)

# Canonical subject token used for first-person / generic-user statements.
SUBJECT_USER = "用户"

# Normalized predicate for preference statements.
PRED_PREFERENCE = "偏好"

# 8.1 pattern (1): positive preference.
#   (我|用户)(喜欢|偏好|爱|习惯)X  ->  (用户, 偏好, X)
_POSITIVE_RE = re.compile(r"^(?:我|用户)(喜欢|偏好|爱|习惯)(.+)$")

# 8.1 pattern (2): negative preference.
#   (我|用户)(不喜欢|讨厌|厌恶)X   ->  (用户, 偏好, X, negation=true)
# NOTE: this must be matched BEFORE the positive rule so that "不喜欢"
# is not swallowed by "喜欢".
_NEGATIVE_RE = re.compile(r"^(?:我|用户)(不喜欢|讨厌|厌恶)(.+)$")

# 8.1 pattern (3): possession / attribute.
#   (我|用户)的X是Y                ->  (用户, X, Y)
_ATTRIBUTE_RE = re.compile(r"^(?:我|用户)的(.+?)是(.+)$", re.UNICODE)

# Procedural: a named workflow / experience with ordered steps.
#   (X)(的)(步骤|流程|做法|经验|套路)是(步骤列表)  ->  (用户, 工作流程名, 步骤, type=procedural)
_PROCEDURAL_RE = re.compile(
    r"^(?:我|用户)?(.+?(?:步骤|流程|做法|经验|套路))是(?:[:：])?(.+)$"
)

# Episodic: a time-anchored event. Matches an optional time word followed by
# an event-producing verb, e.g. "今天 我 完成了发布" -> (用户, 事件, ...).
# The time is captured so the summary can attach a "when" qualifier.
_EPISODIC_RE = re.compile(
    r"^(?:我|用户)?"
    r"(今天|昨天|前天|上周|这周|上个月|这个月|下周|明天|星期[一二三四五六日天]|刚刚|之前|后来|当时)?"
    r"(?:我|用户)?"
    r"(做了|发生了|完成(?:了)?|遇到(?:了)?|参加(?:了)?|经历(?:了)?|写下(?:了)?|读到(?:了)?)"
    r"(.+)$"
)

# Lesson / takeaway.  (我|用户|我们)?(这次|这)?的?(经验|教训|心得|总结)是X
_LESSON_RE = re.compile(
    r"^(?:我|用户|我们)?(?:这次|这)?的?(经验|教训|心得|总结)是(?:[:：])?(.+)$"
)
# SOP: an explicitly named standard operating procedure.
#   (X的)?SOP(是)?Y   /  (X的)标准流程是Y
_SOP_RE = re.compile(
    r"^(?:我|用户)?(.+?)?(?:的)?(?:SOP|标准流程)(?:是)?(?:[:：])?(.+)$"
)
# Decision rule (rule-of-thumb form): 当X时应该/不要/则Y
_DECISION_RULE_RE = re.compile(
    r"^(?:我|用户)?(?:当)?(.+?)(?:时|的时候)(应该|要|不要|必须|则|就)(?:应该|要|必须)?(.+)$"
)

# Ordering markers used to split a procedural object into its steps.
_ORDER_MARKERS = re.compile(r"(?:首先|然后|接着|最后|第一步|第二步|第三步|随后|再)")
# Step separators: Chinese/ASCII punctuation.
_STEP_SEP = re.compile(r"[，,；;。.、\s]+|[0-9]+[).、]")


def extract_rules(
    text: str,
    user_id: str,
    session_id: str,
    turn_id: int = 0,
    raw_text: Optional[str] = None,
) -> List[FactCandidate]:
    """Apply the rule patterns to ``text`` and return extracted candidates.

    White space is stripped before matching. Each rule produces at most one
    candidate; patterns are tried in a fixed priority order and the first
    match wins (negative preference is tried before positive so the
    ``不`` prefix is preserved).

    Args:
        text: The utterance to extract facts from.
        user_id: Owner of the resulting candidates.
        session_id: Origin session.
        turn_id: Origin turn within the session.
        raw_text: Optional original text to store verbatim on the candidate;
            defaults to ``text``.

    Returns:
        A list of :class:`FactCandidate` objects (possibly empty).
    """
    raw = raw_text if raw_text is not None else text
    stripped = (text or "").strip()
    if not stripped:
        return []

    # Negative preference first (priority over positive).
    m = _NEGATIVE_RE.match(stripped)
    if m:
        return [
            _candidate(
                user_id, session_id, turn_id,
                subject=SUBJECT_USER,
                predicate=PRED_PREFERENCE,
                obj=m.group(2).strip(),
                qualifiers={"negation": True},
                importance=0.6,
                raw_text=raw,
            )
        ]

    m = _POSITIVE_RE.match(stripped)
    if m:
        return [
            _candidate(
                user_id, session_id, turn_id,
                subject=SUBJECT_USER,
                predicate=PRED_PREFERENCE,
                obj=m.group(2).strip(),
                importance=0.6,
                raw_text=raw,
            )
        ]

    # Lesson / takeaway: 这次的教训是...  -> (用户, 教训, content)
    # Checked BEFORE the procedural rule because 经验/教训 keywords also
    # appear in the workflow synonym set.
    m = _LESSON_RE.match(stripped)
    if m:
        cate = m.group(1).strip()  # 经验 / 教训 / 心得 / 总结
        body = m.group(2).strip()
        return [
            _candidate(
                user_id, session_id, turn_id,
                subject=SUBJECT_USER,
                predicate=("教训" if "教训" in cate else cate),
                obj=_headline(body),
                qualifiers={"knowledge": True},
                importance=0.8,
                raw_text=raw,
                type=TYPE_LESSON,
                content=body,
            )
        ]

    # SOP: 发布SOP是... / 上线的标准流程是...  -> (用户, SOP名, ...)
    # Checked BEFORE the procedural rule so "标准流程" is not treated as a
    # generic procedural workflow.
    m = _SOP_RE.match(stripped)
    if m:
        name = _clean_workflow_name(m.group(1) or "SOP")
        body = m.group(2).strip()
        return [
            _candidate(
                user_id, session_id, turn_id,
                subject=SUBJECT_USER,
                predicate=name or "SOP",
                obj=_headline(body),
                qualifiers={"knowledge": True},
                importance=0.85,
                raw_text=raw,
                type=TYPE_SOP,
                content=body,
            )
        ]

    m = _DECISION_RULE_RE.match(stripped)
    if m:
        cond = m.group(1).strip()
        act_verb = m.group(2).strip()  # 应该/要/不要...
        act = m.group(3).strip()
        obj = f"当{cond}时 {act_verb} {act}"
        return [
            _candidate(
                user_id, session_id, turn_id,
                subject=SUBJECT_USER,
                predicate="决策规则",
                obj=_headline(obj),
                qualifiers={"knowledge": True},
                importance=0.8,
                raw_text=raw,
                type=TYPE_DECISION_RULE,
                content=obj,
            )
        ]

    # Procedural workflow: named steps / how-to / experience. Checked before
    # the attribute rule so "发布流程是..." is not swallowed as an attribute.
    m = _PROCEDURAL_RE.match(stripped)
    if m:
        workflow = _clean_workflow_name(m.group(1))
        steps = _split_steps(m.group(2))
        return [
            _candidate(
                user_id, session_id, turn_id,
                subject=SUBJECT_USER,
                predicate=workflow or "工作流程",
                obj=_join_steps(steps),
                qualifiers={"steps": steps},
                importance=0.8,
                raw_text=raw,
                type=TYPE_PROCEDURAL,
            )
        ]

    # Episodic event: a time-anchored "I did / this happened" statement.
    m = _EPISODIC_RE.match(stripped)
    if m:
        when = (m.group(1) or "").strip()
        event = m.group(3).strip()
        q = {"episodic": True}
        if when:
            q["when"] = when
        return [
            _candidate(
                user_id, session_id, turn_id,
                subject=SUBJECT_USER,
                predicate=PRED_EVENT,
                obj=event,
                qualifiers=q,
                importance=0.7,
                raw_text=raw,
                type=TYPE_EPISODIC,
            )
        ]

    m = _ATTRIBUTE_RE.match(stripped)
    if m:
        return [
            _candidate(
                user_id, session_id, turn_id,
                subject=SUBJECT_USER,
                predicate=m.group(1).strip(),
                obj=m.group(2).strip(),
                importance=0.6,
                raw_text=raw,
            )
        ]

    return []


def _headline(text: str, limit: int = 60) -> str:
    """Truncate a knowledge body into a short searchable title."""
    t = (text or "").strip()
    if len(t) <= limit:
        return t
    return t[:limit] + "…"


def _clean_workflow_name(name: str) -> str:
    """Normalize a captured workflow name.

    Strips a leading first-person prefix (我 / 用户 / 的) that the pattern may
    capture, returning e.g. ``"发布流程"`` from ``"我的发布流程"``.

    Args:
        name: Raw captured workflow name.

    Returns:
        A stripped, de-prefixed workflow name.
    """
    n = (name or "").strip()
    n = re.sub(r"^(?:我|用户|的)+", "", n).strip()
    return n


def _split_steps(text: str) -> List[str]:
    """Split a procedural body into an ordered list of steps.

    Steps are delimited by Chinese/ASCII punctuation, numbered markers
    (``1.``, ``1)`` ...) or ordering words (首先/然后/最后/第一步...). The
    pattern is intentionally conservative: if no clean boundary is found the
    body is returned as a single step.

    Args:
        text: Raw step text extracted by the procedural rule.

    Returns:
        A non-empty list of stripped step strings.
    """
    if not text or not text.strip():
        return []
    # First separate on ordering words so "首先A然后B最后C" -> [A, B, C].
    segments = _ORDER_MARKERS.split(text)
    pieces: List[str] = []
    for seg in segments:
        pieces.extend(
            t for t in (s.strip() for s in _STEP_SEP.split(seg)) if t
        )
    return pieces or [text.strip()]


def _join_steps(steps: List[str]) -> str:
    """Join an ordered step list back into a compact object string."""
    return " -> ".join(steps)


def _candidate(
    user_id: str,
    session_id: str,
    turn_id: int,
    subject: str,
    predicate: str,
    obj: str,
    importance: float,
    raw_text: str,
    qualifiers: Optional[dict] = None,
    type: str = TYPE_SEMANTIC,
    content: Optional[str] = None,
) -> FactCandidate:
    """Build a :class:`FactCandidate` from extracted parts."""
    import uuid
    import json

    return FactCandidate(
        candidate_id=str(uuid.uuid4()),
        user_id=user_id,
        session_id=session_id,
        turn_id=turn_id,
        subject=subject,
        predicate=predicate,
        object=obj,
        qualifiers=json.dumps(qualifiers, ensure_ascii=False) if qualifiers else None,
        confidence=0.7,
        importance=importance,
        privacy="private",
        raw_text=raw_text,
        type=type,
        content=content,
    )


class Extractor:
    """Extracts atomic fact candidates from user utterances.

    Combines rule-based extraction with an optional pluggable LLM extractor.
    When an LLM extractor is configured it runs first and its non-empty
    result is authoritative; the rule engine is the fallback so an utterance
    is never silently dropped when the LLM path is unavailable or empty.
    """

    def __init__(self, llm_extractor: Optional[Callable[..., list]] = None) -> None:
        """Initialise the extractor.

        Args:
            llm_extractor: Optional callable invoked with
                ``(text, user_id, session_id, turn_id)`` for every utterance,
                expected to return a list of :class:`FactCandidate` instances
                or a list of dicts with SPO-like keys. When ``None`` only the
                rule engine runs. When present it runs first; its non-empty
                result is authoritative and the rule engine is only the
                fallback (LLM threw or returned nothing usable).
        """
        self.llm_extractor = llm_extractor

    def extract(
        self,
        text: str,
        user_id: str,
        session_id: str,
        turn_id: int = 0,
    ) -> List[FactCandidate]:
        """Extract candidates from a single utterance, **LLM-first**.

        When an ``llm_extractor`` is configured it runs first and, if it
        yields at least one usable candidate, its result is authoritative and
        returned as-is. The rule engine is the fallback: it runs only when the
        LLM path is unavailable (no extractor configured), throws, or returns
        no usable candidates. This keeps extraction accurate (LLM) without
        ever silently dropping an utterance (rules).

        Duplicate candidates are not removed here — deduplication is handled
        downstream by the idempotency key.

        Args:
            text: The utterance text.
            user_id: Owner of the candidates.
            session_id: Origin session.
            turn_id: Origin turn.

        Returns:
            A list of extracted :class:`FactCandidate` objects.
        """
        if self.llm_extractor is not None:
            llm_candidates = self._try_llm(text, user_id, session_id, turn_id)
            if llm_candidates:
                return llm_candidates

        return extract_rules(text, user_id, session_id, turn_id, raw_text=text)

    def _try_llm(
        self,
        text: str,
        user_id: str,
        session_id: str,
        turn_id: int,
    ) -> List[FactCandidate]:
        """Invoke the configured LLM extractor and normalize its output.

        Any exception raised by the LLM extractor (timeout, provider error,
        malformed output...) is swallowed here so it degrades to rule
        fallback instead of breaking the write pipeline. Dict-style candidates
        that omit their owner are stamped with the current ``user_id`` /
        ``session_id`` / ``turn_id`` so facts are always correctly scoped.

        Returns:
            A non-empty list of candidates, or ``[]`` when the LLM threw or
            returned nothing usable.
        """
        try:
            extra = self.llm_extractor(text, user_id, session_id, turn_id)
        except Exception:
            logger.warning("LLM extractor failed; falling back to rules", exc_info=True)
            return []
        return _normalize_llm_output(extra, user_id, session_id, turn_id)


def _normalize_llm_output(extra, user_id: str, session_id: str, turn_id: int) -> List[FactCandidate]:
    """Normalize an LLM extractor's raw output into candidates.

    Accepts ``None``, a single candidate, or a sequence of candidates where
    each is either a :class:`FactCandidate` or a dict with SPO-like keys.
    ``FactCandidate`` instances are returned as-is; dicts are stamped with the
    current ``user_id`` / ``session_id`` / ``turn_id`` when they omit them.
    Blank / degenerate entries (e.g. a dict missing a predicate) are dropped;
    the result is deduplicated by ``candidate_id``.

    Args:
        extra: The raw value returned by the configured LLM extractor.
        user_id: Owner to stamp on dict candidates that omit it.
        session_id: Session to stamp on dict candidates that omit it.
        turn_id: Turn to stamp on dict candidates that omit it.

    Returns:
        A (possibly empty) list of :class:`FactCandidate` objects.
    """
    if extra is None:
        return []
    items = extra if isinstance(extra, (list, tuple)) else [extra]

    out: List[FactCandidate] = []
    seen: set = set()
    for item in items:
        if isinstance(item, FactCandidate):
            cand = item
        elif isinstance(item, dict):
            cand = _candidate_from_dict(item, user_id, session_id, turn_id)
        else:
            continue
        if cand.predicate or cand.idempotency_key:
            if cand.candidate_id not in seen:
                seen.add(cand.candidate_id)
                out.append(cand)
    return out


def _candidate_from_dict(d: dict, user_id: str = "", session_id: str = "", turn_id: int = 0) -> FactCandidate:
    """Build a :class:`FactCandidate` from a partial dict.

    Args:
        d: A mapping with any of subject / predicate / object / qualifiers /
            confidence / importance / privacy / idempotency_key / type keys.

    Returns:
        A populated candidate with generated id.
    """
    import uuid

    subject = d.get("subject")
    predicate = d.get("predicate")
    obj = d.get("object")
    qualifiers = d.get("qualifiers")
    if isinstance(qualifiers, (dict, list)):
        import json

        qualifiers = json.dumps(qualifiers, ensure_ascii=False)
    return FactCandidate(
        candidate_id=str(uuid.uuid4()),
        user_id=d.get("user_id") or user_id,
        session_id=d.get("session_id") or session_id,
        turn_id=int(d.get("turn_id", turn_id) or turn_id),
        subject=subject,
        predicate=predicate,
        object=obj,
        qualifiers=qualifiers,
        confidence=d.get("confidence", 0.5),
        importance=d.get("importance", 0.5),
        privacy=d.get("privacy", "private"),
        raw_text=d.get("raw_text"),
        idempotency_key=d.get("idempotency_key"),
        type=d.get("type", TYPE_SEMANTIC),
        content=d.get("content"),
    )
