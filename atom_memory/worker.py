"""Asyncio worker that drains the ``task_queue``.

The worker owns the write-side of the pipeline. It polls ``task_queue``,
dispatches each task by ``task_type``, retries failures with exponential
backoff and marks permanently-failing tasks as ``dead`` (recording an
``events`` row and emitting an error log).

Stage 2 implements the ``extract`` handler: extract candidates from the raw
utterance, run the validation chain, then persist accepted facts into the
``facts``, ``facts_fts`` and ``facts_vec`` tables.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from typing import Callable, List, Optional

from .db import now_ms
from .models import AtomicFact, FactCandidate
from .retriever import segment_text
from .summarizer import SCOPE_GLOBAL, mark_stale, try_rebuild
from .validator import validate

logger = logging.getLogger(__name__)

# Candidate lifecycle statuses.
CAND_STATUS_APPLIED = "applied"
CAND_STATUS_SKIPPED = "skipped"
CAND_STATUS_ERROR = "error"

# Task statuses.
TASK_PENDING = "pending"
TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_DEAD = "dead"


def _candidate_from_rpc_dict(
    d: dict,
    user_id: str,
    session_id: str,
    turn_id: int,
) -> FactCandidate:
    """Build a :class:`FactCandidate` from an RPC candidate dict.

    Used by the ``persist_pre`` worker task to accept candidates that the dsh
    host already extracted (LLM-first) so they can be validated and persisted
    through the same chain as rule-extracted candidates.

    Args:
        d: A dict with any of subject / predicate / object / type / content /
            qualifiers / confidence / importance / privacy keys.
        user_id: Owner to stamp when the dict omits it.
        session_id: Session to stamp when the dict omits it.
        turn_id: Turn to stamp when the dict omits it.

    Returns:
        A populated candidate with a generated id.
    """
    import json as _json

    quals = d.get("qualifiers")
    if isinstance(quals, (dict, list)):
        quals = _json.dumps(quals, ensure_ascii=False)
    return FactCandidate(
        candidate_id=str(uuid.uuid4()),
        user_id=d.get("user_id") or user_id,
        session_id=d.get("session_id") or session_id,
        turn_id=int(d.get("turn_id", turn_id) or turn_id),
        subject=d.get("subject"),
        predicate=d.get("predicate"),
        object=d.get("object"),
        qualifiers=quals,
        confidence=d.get("confidence", 0.5),
        importance=d.get("importance", 0.5),
        privacy=d.get("privacy", "private"),
        raw_text=d.get("raw_text"),
        idempotency_key=d.get("idempotency_key"),
        type=d.get("type", "semantic"),
        content=d.get("content"),
    )


class Worker:
    """Polling worker for the memory task queue."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        embed_func: Callable[[str], bytes],
        poll_interval_sec: float = 0.5,
        max_retries: int = 3,
        llm_extractor: Optional[Callable[..., list]] = None,
        privacy_filter: str = "private",
        summary_debounce_sec: float = 30.0,
        summary_max_tokens: int = 1500,
    ) -> None:
        """Initialise the worker.

        Args:
            conn: The SQLite connection (single writer).
            embed_func: Callable mapping a text string to a serialized
                embedding BLOB. Must be safe to call from worker threads.
            poll_interval_sec: Seconds between queue polls.
            max_retries: Max retries before a task is marked dead.
            llm_extractor: Optional LLM extractor callable.
            privacy_filter: Default privacy tag applied during validation.
            summary_debounce_sec: Debounce for debounced summary rebuilds.
            summary_max_tokens: Token cap for rebuilt summaries.
        """
        self.conn = conn
        self.embed_func = embed_func
        self.poll_interval_sec = poll_interval_sec
        self.max_retries = max_retries

        from .extractor import Extractor

        self.extractor = Extractor(llm_extractor=llm_extractor)
        self.privacy_filter = privacy_filter
        self.summary_debounce_sec = summary_debounce_sec
        self.summary_max_tokens = summary_max_tokens
        self._task: Optional[asyncio.Task] = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Begin draining the queue in a background asyncio task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="dsh-worker")

    async def stop(self) -> None:
        """Stop the worker and await graceful shutdown."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    # -- main loop -----------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                task_row = self._claim_next_task()
                if task_row is None:
                    await asyncio.sleep(self.poll_interval_sec)
                    continue
                await self._handle_task(task_row)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("Worker loop error: %s", exc)
                await asyncio.sleep(self.poll_interval_sec)

    # -- queue -----------------------------------------------------------------

    def _claim_next_task(self) -> Optional[sqlite3.Row]:
        """Atomically claim the highest-priority pending task.

        Returns:
            The claimed task row, or ``None`` if the queue is empty.
        """
        if self.conn is None:
            return None
        row = self.conn.execute(
            "SELECT task_id, task_type, payload, retry_count, max_retries "
            "FROM task_queue "
            "WHERE status = ? "
            "ORDER BY priority, created_at "
            "LIMIT 1",
            (TASK_PENDING,),
        ).fetchone()
        if row is None:
            return None
        # Mark as running so concurrent consumers don't double-dispatch; the
        # status is restored to pending if the task is later retried.
        self.conn.execute(
            "UPDATE task_queue SET status = ?, started_at = ? WHERE task_id = ?",
            (TASK_RUNNING, now_ms(), row["task_id"]),
        )
        self.conn.commit()
        return row

    async def _handle_task(self, row: sqlite3.Row) -> None:
        task_id = row["task_id"]
        task_type = row["task_type"]
        payload = json.loads(row["payload"]) if row["payload"] else {}

        try:
            if task_type == "extract":
                await self._process_extract(payload)
            elif task_type == "replace":
                await self._process_replace(payload)
            elif task_type == "forget":
                await self._process_forget(payload)
            elif task_type == "persist_pre":
                await self._process_persist_pre(payload)
            elif task_type == "rebuild_md":
                await self._process_rebuild_md(payload)
            else:
                raise ValueError(f"unknown task_type: {task_type}")

            self.conn.execute(
                "UPDATE task_queue SET status = ?, completed_at = ? "
                "WHERE task_id = ?",
                (TASK_DONE, now_ms(), task_id),
            )
            self.conn.commit()
        except Exception as exc:
            await self._record_failure(task_id, row["retry_count"], exc)

    async def _record_failure(
        self, task_id: str, retry_count: int, exc: Exception
    ) -> None:
        """Apply the retry / dead policy after a failed task.

        A task is allowed ``max_retries`` attempts in total; after that many
        failures it is marked ``dead``. Retries in between are spaced by an
        exponential backoff (1s, 2s, 4s, ... capped at 60s).
        """
        retry_count = int(retry_count) + 1
        if retry_count >= self.max_retries:
            self.conn.execute(
                "UPDATE task_queue SET status = ?, retry_count = ?, error = ?, "
                "completed_at = ? WHERE task_id = ?",
                (TASK_DEAD, retry_count, str(exc)[:2000], now_ms(), task_id),
            )
            self.conn.commit()
            self._log_dead(task_id, exc)
            return

        # Requeue for the next poll after the backoff delay.
        delay = min(2 ** (retry_count - 1), 60)
        self.conn.execute(
            "UPDATE task_queue SET status = ?, retry_count = ?, error = ? "
            "WHERE task_id = ?",
            (TASK_PENDING, retry_count, str(exc)[:2000], task_id),
        )
        self.conn.commit()
        logger.warning(
            "Task %s failed (%d/%d): %s; retrying in %ss",
            task_id, retry_count, self.max_retries, exc, delay,
        )
        # Sleep the backoff *outside* the claim loop by yielding to the loop.
        await asyncio.sleep(delay)

    def _log_dead(self, task_id: str, exc: Exception) -> None:
        """Mark a dead task, log an alarm and record an event."""
        logger.error("Task %s permanently failed (dead): %s", task_id, exc)
        try:
            self.conn.execute(
                "INSERT INTO events(event_id, user_id, type, payload, trace_id, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    str(uuid.uuid4()),
                    "",
                    "task_dead",
                    json.dumps({"task_id": task_id, "error": str(exc)[:2000]}),
                    None,
                    now_ms(),
                ),
            )
            self.conn.commit()
        except Exception:  # pragma: no cover - event write must not raise
            logger.exception("Failed to record dead-task event for %s", task_id)

    # -- extract handler ---------------------------------------------------------

    async def _process_extract(self, payload: dict) -> None:
        """Run extraction + validation + persistence for an extract task.

        Args:
            payload: The task payload (candidate_id, user_id, session_id,
                turn_id, raw_text).
        """
        candidate_id = payload["candidate_id"]
        user_id = payload["user_id"]
        session_id = payload["session_id"]
        turn_id = int(payload.get("turn_id", 0))
        text = payload.get("raw_text", "")

        candidates = self.extractor.extract(text, user_id, session_id, turn_id)

        if not candidates:
            self._set_candidate_status(candidate_id, CAND_STATUS_SKIPPED)
            return

        persisted = False
        for candidate in candidates:
            result = validate(
                candidate, self.conn, privacy_filter=self.privacy_filter
            )
            if not result.ok:
                # Conflicts and idempotent duplicates are not written; other
                # validation failures are skipped with a debug log.
                logger.debug(
                    "Candidate %s rejected (%s): %s",
                    candidate.candidate_id, result.kind, result.reason,
                )
                continue
            await self._persist_fact(candidate, trace_id=None)
            persisted = True

        self._set_candidate_status(candidate_id, CAND_STATUS_APPLIED)
        if persisted:
            self._after_mutation(user_id)

    async def _process_persist_pre(self, payload: dict) -> None:
        """Persist pre-extracted candidates (e.g. from the dsh-side LLM extractor).

        The dsh host runs LLM-first extraction in its own process (where
        ``ctx.llm`` lives) and ships the resulting typed candidates here via the
        ``persist_pre`` task. Each candidate dict is turned into a
        :class:`FactCandidate` and pushed through the *same* validation +
        persistence chain as rule extraction, so conflict resolution and
        idempotency (identical-SPO suppression) still apply.

        Args:
            payload: keys ``candidate_id``, ``user_id``, ``session_id``,
                ``turn_id``, ``candidates`` (list of dicts with subject /
                predicate / object / type / content / qualifiers ...).
        """
        candidate_id = payload.get("candidate_id") or str(uuid.uuid4())
        user_id = payload["user_id"]
        session_id = payload.get("session_id", "s_default")
        turn_id = int(payload.get("turn_id", 0))
        candidates = payload.get("candidates") or []

        persisted = False
        for d in candidates:
            cand = _candidate_from_rpc_dict(d, user_id, session_id, turn_id)
            result = validate(cand, self.conn, privacy_filter=self.privacy_filter)
            if not result.ok:
                logger.debug(
                    "Pre-extracted candidate %s rejected (%s): %s",
                    cand.candidate_id, result.kind, result.reason,
                )
                continue
            await self._persist_fact(cand, trace_id=None)
            persisted = True

        self._set_candidate_status(candidate_id, CAND_STATUS_APPLIED)
        if persisted:
            self._after_mutation(user_id)

    def _set_candidate_status(self, candidate_id: str, status: str) -> None:
        """Update a fact_candidates row's status."""
        self.conn.execute(
            "UPDATE fact_candidates SET status = ? WHERE candidate_id = ?",
            (status, candidate_id),
        )
        self.conn.commit()

    async def _persist_fact(self, candidate: FactCandidate, trace_id: Optional[str]) -> None:
        """Persist a validated candidate into facts + facts_fts + facts_vec.

        Args:
            candidate: The validated candidate.
            trace_id: Optional trace id recorded with the fact.
        """
        fact_id = str(uuid.uuid4())
        text = f"{candidate.subject} {candidate.predicate} {candidate.object}"
        content = candidate.content or ""
        searchable = (text + " " + content).strip()
        created_at = now_ms()

        # Embedding is CPU-bound; run in a worker thread so the loop stays
        # responsive during model inference.
        blob = await asyncio.to_thread(self.embed_func, searchable)

        self.conn.execute(
            "INSERT INTO facts(fact_id, user_id, session_id, subject, predicate, "
            "object, qualifiers, confidence, importance, privacy, source_type, "
            "status, superseded_by, observed_at, created_at, trace_id, version, type, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fact_id,
                candidate.user_id,
                candidate.session_id,
                candidate.subject,
                candidate.predicate,
                candidate.object,
                candidate.qualifiers,
                candidate.confidence,
                candidate.importance,
                candidate.privacy,
                "user_explicit",
                "active",
                None,
                created_at,
                created_at,
                trace_id,
                1,
                getattr(candidate, "type", "semantic") or "semantic",
                content or None,
            ),
        )
        # FTS index uses jieba-segmented text so Chinese queries can match
        # individual words (unicode61 treats a CJK span as a single token).
        # The body content is indexed too so knowledge facts are findable by
        # their full text, not only the SPO title.
        self.conn.execute(
            "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
            (fact_id, " ".join(segment_text(searchable))),
        )
        self.conn.execute(
            "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
            (fact_id, blob),
        )
        self.conn.commit()
        return fact_id

    # -- mutation bookkeeping -----------------------------------------------------

    def _after_mutation(self, user_id: str) -> None:
        """Mark summaries stale and schedule a debounced summary rebuild.

        Called after any fact mutation so derived views (summaries) are
        flagged and eventually regenerated.
        """
        mark_stale(self.conn, user_id)
        self._enqueue("rebuild_md", user_id, {"user_id": user_id})

    def _enqueue(self, task_type: str, user_id: str, payload: dict) -> str:
        """Insert a task into the queue and return its id."""
        task_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO task_queue(task_id, task_type, payload, status, "
            "priority, retry_count, max_retries, created_at) "
            "VALUES (?, ?, ?, 'pending', 5, 0, ?, ?)",
            (
                task_id,
                task_type,
                json.dumps(payload),
                self.max_retries,
                now_ms(),
            ),
        )
        self.conn.commit()
        return task_id

    def _supersede(self, old_fact_id: str, new_fact_id: str) -> None:
        """Soft-replace: mark ``old_fact_id`` superseded by ``new_fact_id``."""
        self.conn.execute(
            "UPDATE facts SET status = 'superseded', superseded_by = ? "
            "WHERE fact_id = ?",
            (new_fact_id, old_fact_id),
        )
        self.conn.commit()

    def _retract(self, fact_id: str) -> None:
        """Soft-delete: mark a fact ``retracted``."""
        self.conn.execute(
            "UPDATE facts SET status = 'retracted' WHERE fact_id = ?",
            (fact_id,),
        )
        self.conn.commit()

    # -- replace handler -----------------------------------------------------------

    async def _process_replace(self, payload: dict) -> None:
        """Handle a replace task: persist the new fact(s), supersede the old.

        Args:
            payload: keys candidate_id, user_id, old_fact_id, new_text,
                session_id, turn_id.
        """
        candidate_id = payload["candidate_id"]
        user_id = payload["user_id"]
        old_fact_id = payload["old_fact_id"]
        text = payload.get("new_text", "")
        session_id = payload.get("session_id", "s_default")
        turn_id = int(payload.get("turn_id", 0))

        # Only replace an active fact owned by the user.
        old = self.conn.execute(
            "SELECT fact_id FROM facts WHERE user_id = ? AND fact_id = ? "
            "AND status = 'active'",
            (user_id, old_fact_id),
        ).fetchone()
        if old is None:
            self._set_candidate_status(candidate_id, CAND_STATUS_SKIPPED)
            return

        candidates = self.extractor.extract(text, user_id, session_id, turn_id)
        new_ids: List[str] = []
        for candidate in candidates:
            result = validate(candidate, self.conn, privacy_filter=self.privacy_filter)
            if not result.ok:
                logger.debug("replace candidate rejected (%s)", result.kind)
                continue
            new_ids.append(await self._persist_fact(candidate, trace_id=None))

        if new_ids:
            self._supersede(old_fact_id, new_ids[0])
            self._set_candidate_status(candidate_id, CAND_STATUS_APPLIED)
            self._after_mutation(user_id)
        else:
            self._set_candidate_status(candidate_id, CAND_STATUS_SKIPPED)

    # -- forget handler ---------------------------------------------------------------

    async def _process_forget(self, payload: dict) -> None:
        """Handle a forget task: soft-delete the target fact(s).

        Args:
            payload: keys candidate_id, user_id, fact_id (optional),
                session_id (optional).
        """
        candidate_id = payload["candidate_id"]
        user_id = payload["user_id"]
        fact_id = payload.get("fact_id")
        session_id = payload.get("session_id")

        if fact_id:
            # Honour user isolation: retract any non-retracted fact of the
            # user (active or superseded both leave memory).
            self.conn.execute(
                "UPDATE facts SET status = 'retracted' "
                "WHERE user_id = ? AND fact_id = ? AND status IN "
                "('active', 'superseded')",
                (user_id, fact_id),
            )
        elif session_id:
            self.conn.execute(
                "UPDATE facts SET status = 'retracted' "
                "WHERE user_id = ? AND session_id = ? AND status IN "
                "('active', 'superseded')",
                (user_id, session_id),
            )
        self.conn.commit()

        self._set_candidate_status(candidate_id, CAND_STATUS_APPLIED)
        self._after_mutation(user_id)

    # -- rebuild_md handler -----------------------------------------------------------

    async def _process_rebuild_md(self, payload: dict) -> None:
        """Handle a rebuild_md task: debounced summary regeneration.

        Args:
            payload: keys user_id.
        """
        user_id = payload.get("user_id")
        if not user_id:
            raise ValueError("rebuild_md payload missing user_id")
        try_rebuild(
            self.conn,
            user_id,
            scope=SCOPE_GLOBAL,
            debounce_sec=self.summary_debounce_sec,
            max_tokens=self.summary_max_tokens,
        )
