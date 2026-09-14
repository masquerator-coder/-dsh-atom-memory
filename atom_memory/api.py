"""Public ``AtomMem`` API — the in-process memory front door.

Exposes the full write/read/mutation surface:

    lifecycle  — start() / stop()
    write      — add(user, session, text)
    read       — recall(), memory_md(), user_md(), summary()
    mutate     — replace(), forget()
    metrics    — stats()
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from typing import Optional

from .backup import export_memory, import_memory, validate_backup
from .config import MemConfig
from .db import now_ms, open_db
from .embedder import Embedder
from .memory_md import generate_memory_md
from .models import SUMMARY_EXCLUDED_KNOWLEDGE
from .profile import derive_profile_from_facts, profile_md
from .retriever import Retriever, estimate_tokens, segment_text
from .summarizer import SCOPE_GLOBAL, rebuild_summary
from .worker import Worker

logger = logging.getLogger(__name__)


def _parse_fact_ids(raw) -> list:
    """Parse the ``summaries.fact_ids`` JSON column into a list of ids."""
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    return [str(x) for x in parsed] if isinstance(parsed, list) else []


class AtomMem:
    """In-process long-term memory for DeepSeek Harness."""

    def __init__(self, config: MemConfig) -> None:
        """Initialise a memory instance.

        Args:
            config: Library configuration.
        """
        self.config = config
        self.db: Optional[sqlite3.Connection] = None
        self.embedder: Optional[Embedder] = None
        self.retriever: Optional[Retriever] = None
        self._worker: Optional[Worker] = None
        self._started = False

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Open the database, load the embedder and start the worker.

        Idempotent: calling ``start`` more than once has no further effect.
        """
        if self._started:
            return
        self.db = open_db(self.config)
        self.embedder = Embedder(
            model_name=self.config.embedding_model,
            dim=self.config.embedding_dim,
        )
        self.retriever = Retriever(
            conn=self.db,
            embed_one=self.embedder.embed_one,
            top_k_default=10,
        )
        self._worker = Worker(
            conn=self.db,
            embed_func=self.embedder.embed_one,
            poll_interval_sec=self.config.worker_poll_interval_sec,
            max_retries=self.config.max_retries,
            llm_extractor=self.config.llm_extractor,
            privacy_filter=self.config.privacy_filter,
            summary_debounce_sec=self.config.summary_rebuild_debounce_sec,
            summary_max_tokens=self.config.memory_md_token_limit,
        )
        self._worker.start()
        self._started = True
        logger.info("AtomMem started (db=%s)", self.config.resolved_db_path())

    async def stop(self) -> None:
        """Stop the worker and close the database.

        Idempotent: calling ``stop`` after the memory is already stopped is a
        no-op.
        """
        if not self._started:
            return
        if self._worker is not None:
            await self._worker.stop()
        if self.db is not None:
            try:
                self.db.close()
            except sqlite3.Error:  # pragma: no cover - best-effort close
                pass
            self.db = None
        self._started = False
        self._worker = None
        logger.info("AtomMem stopped")

    # -- write pipeline ----------------------------------------------------------

    async def add(
        self,
        user_id: str,
        session_id: str,
        text: str,
        turn_id: int = 0,
    ) -> dict:
        """Enqueue a user utterance to be extracted into atomic facts.

        The utterance is recorded immediately as a pending candidate and an
        ``extract`` task is queued; the worker persists extracted facts
        asynchronously.

        Args:
            user_id: The user who produced the utterance (isolation scope).
            session_id: The session the utterance belongs to.
            text: The raw utterance text.
            turn_id: Optional zero-based turn number.

        Returns:
            A dict ``{"candidate_id", "status", "trace_id"}`` where
            ``status`` is ``"pending"``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")

        candidate_id = str(uuid.uuid4())
        trace_id = str(uuid.uuid4())
        created_at = now_ms()

        with self.db:
            self.db.execute(
                "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
                "turn_id, raw_text, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (candidate_id, user_id, session_id, turn_id, text, created_at),
            )
            self.db.execute(
                "INSERT INTO task_queue(task_id, task_type, payload, status, "
                "priority, retry_count, max_retries, created_at) "
                "VALUES (?, 'extract', ?, 'pending', 5, 0, ?, ?)",
                (
                    str(uuid.uuid4()),
                    json.dumps(
                        {
                            "candidate_id": candidate_id,
                            "user_id": user_id,
                            "session_id": session_id,
                            "turn_id": turn_id,
                            "raw_text": text,
                        }
                    ),
                    self.config.max_retries,
                    created_at,
                ),
            )

        return {"candidate_id": candidate_id, "status": "pending", "trace_id": trace_id}

    # -- recall pipeline ---------------------------------------------------------

    async def recall(
        self,
        user_id: str,
        query: str,
        token_budget: int = 2000,
        top_k: int = 10,
        include_pending: bool = True,
    ) -> dict:
        """Retrieve the user's most relevant active facts for a query.

        Runs FTS + vector retrieval, RRF fusion and re-ranking, then trims the
        result to ``token_budget``. Optionally attaches still-pending candidate
        utterances (reported by ``candidate_id`` only, never ``fact_id``).

        Args:
            user_id: The user whose memory is searched.
            query: The natural-language query.
            token_budget: Upper bound on the estimated tokens returned.
            top_k: Maximum number of facts to consider before trimming.
            include_pending: Whether to include not-yet-persisted candidates.

        Returns:
            A dict with keys ``facts``, ``summaries``, ``pending``,
            ``conflicts``, ``token_count`` and ``trace_id``.
        """
        if self.db is None or self.retriever is None:
            raise RuntimeError("AtomMem is not started; call start() first")

        ranked = await self.retriever.search(user_id, query, top_k=top_k)

        facts: list = []
        used = 0
        for fact in ranked:
            line = f"{fact['subject']}{fact['predicate']}{fact['object']}"
            body = fact.get("content") or ""
            # Budget the knowledge body too: it is delivered to the model (the
            # recall render prints it), so counting only the SPO line let a
            # handful of long SOPs blow far past the requested budget.
            t = estimate_tokens(line) + estimate_tokens(body)
            if used + t > token_budget and facts:
                break
            facts.append(
                {
                    "fact_id": fact["fact_id"],
                    "subject": fact["subject"],
                    "predicate": fact["predicate"],
                    "object": fact["object"],
                    "type": fact.get("type", "semantic"),
                    "content": fact.get("content"),
                    "confidence": fact["confidence"],
                    "importance": fact["importance"],
                    "final_score": fact["final_score"],
                    "status": fact["status"],
                }
            )
            used += t

        pending = (
            self._load_pending(user_id) if include_pending else []
        )

        # Reads are authoritative: the write path debounces rebuilds and never
        # reschedules a deferred one, so a summary can sit stale indefinitely.
        # Refresh it here so recall always carries current content.
        self._ensure_summary(user_id)
        summaries, summary_tokens = self._load_summaries(user_id)

        return {
            "facts": facts,
            "summaries": summaries,
            "pending": pending,
            "conflicts": [],
            "token_count": used + summary_tokens,
            "trace_id": str(uuid.uuid4()),
        }

    def _load_summaries(self, user_id: str) -> tuple:
        """Load the user's fresh (non-stale) summaries for recall.

        Returns:
            A ``(summaries, token_count)`` tuple where each summary has
            ``summary_id``, ``scope``, ``theme``, ``text``, ``fact_ids`` and
            ``version``.
        """
        rows = self.db.execute(
            "SELECT summary_id, scope, theme, text, fact_ids, version "
            "FROM summaries WHERE user_id = ? AND stale = 0",
            (user_id,),
        ).fetchall()
        summaries = []
        tokens = 0
        for r in rows:
            import json as _json

            summaries.append(
                {
                    "summary_id": r["summary_id"],
                    "scope": r["scope"],
                    "theme": r["theme"],
                    "text": r["text"],
                    "fact_ids": r["fact_ids"],
                    "version": r["version"],
                }
            )
            tokens += estimate_tokens(r["text"])
        return summaries, tokens

    def _load_pending(self, user_id: str) -> list:
        """Load still-pending fact candidates for a user (candidate_id only)."""
        rows = self.db.execute(
            "SELECT candidate_id, subject, predicate, object, status "
            "FROM fact_candidates WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchall()
        pending = []
        for r in rows:
            if not all((r["subject"], r["predicate"], r["object"])):
                continue
            pending.append(
                {
                    "candidate_id": r["candidate_id"],
                    "subject": r["subject"],
                    "predicate": r["predicate"],
                    "object": r["object"],
                    "status": r["status"],
                }
            )
        return pending

    async def memory_md(self, user_id: str, max_tokens: int = 1500) -> str:
        """Render the user's ``memory.md`` derived view.

        Args:
            user_id: The user whose memory is rendered.
            max_tokens: Estimated token cap for the body.

        Returns:
            A markdown string listing active facts with their ``fact_id``s.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        return generate_memory_md(self.db, user_id, max_tokens)

    async def user_md(self, user_id: str, max_tokens: int = 800) -> str:
        """Render the user's profile as markdown.

        The profile is (re)derived from active facts before rendering.

        Args:
            user_id: The user whose profile is rendered.
            max_tokens: Estimated token cap for the body.

        Returns:
            A markdown string describing the user's profile.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        derive_profile_from_facts(self.db, user_id)
        return profile_md(self.db, user_id, max_tokens)

    async def summary(self, user_id: str) -> str:
        """Render the user's aggregate summary as markdown.

        The summary is a compact, lossy digest of the active facts — stable
        attributes, preferences, workflows, recent events and light knowledge —
        intended as the cheap "look first, then drill in with recall" view. It
        is refreshed first when missing or stale.

        The rendering carries the covered ``fact_id``s and an explicit note
        about long-form knowledge (SOP / few-shot) whose body was deliberately
        left out of the digest, so a reader can see *that* there is detail to
        drill into and *which* facts hold it.

        Args:
            user_id: The user whose summary is rendered.

        Returns:
            A markdown string. Contains an explicit empty notice when the user
            has no active facts yet.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        self._ensure_summary(user_id)
        rows = self.db.execute(
            "SELECT scope, theme, text, version, fact_ids FROM summaries "
            "WHERE user_id = ? AND stale = 0 ORDER BY scope",
            (user_id,),
        ).fetchall()
        if not rows:
            return (
                f"# 摘要 (Summary) — {user_id}\n\n"
                "_暂无摘要。_ (No summary yet — no active facts.)\n"
            )
        lines = [f"# 摘要 (Summary) — {user_id}", ""]
        for r in rows:
            fact_ids = _parse_fact_ids(r["fact_ids"])
            lines.append(f"## {r['theme'] or r['scope']} (v{r['version']})")
            lines.append("")
            lines.append(r["text"])
            lines.append("")
            excluded = self._excluded_knowledge_ids(user_id, fact_ids)
            if excluded:
                lines.append(
                    f"> ⚠ 另有 {len(excluded)} 条长文知识（SOP/few-shot）未展开正文，"
                    f"需要时用 memory_recall 检索，或直接查看 fact_id: "
                    f"{', '.join(excluded)}"
                )
            lines.append(
                f"> 覆盖 {len(fact_ids)} 条活跃事实 · fact_id: {', '.join(fact_ids) or '（无）'}"
            )
        return "\n".join(lines)

    def _excluded_knowledge_ids(self, user_id: str, fact_ids: list) -> list:
        """Return the subset of ``fact_ids`` whose type is long-form knowledge.

        Those bodies are intentionally absent from the summary text, so the
        caller surfaces their ids as drill-down pointers.

        Args:
            user_id: Owner of the facts.
            fact_ids: Fact ids covered by the summary.

        Returns:
            Fact ids whose ``type`` is in ``SUMMARY_EXCLUDED_KNOWLEDGE``.
        """
        if self.db is None or not fact_ids:
            return []
        placeholders = ",".join("?" for _ in fact_ids)
        rows = self.db.execute(
            f"SELECT fact_id, type FROM facts WHERE user_id = ? "
            f"AND fact_id IN ({placeholders})",
            (user_id, *fact_ids),
        ).fetchall()
        return [
            r["fact_id"]
            for r in rows
            if (r["type"] or "semantic") in SUMMARY_EXCLUDED_KNOWLEDGE
        ]

    def _ensure_summary(self, user_id: str) -> None:
        """Rebuild the user's summary when it is missing or stale.

        The write path (``_after_mutation``) marks summaries stale and enqueues
        a debounced rebuild, but a rebuild deferred by the debounce is never
        rescheduled — so a summary can remain stale forever once mutations stop.
        Read paths call this to guarantee current content. The aggregate is pure
        in-process string work over the facts table (no model call), so doing it
        on demand is cheap.

        Args:
            user_id: The user whose summary should be current.
        """
        if self.db is None:
            return
        row = self.db.execute(
            "SELECT stale FROM summaries WHERE user_id = ? AND scope = ?",
            (user_id, SCOPE_GLOBAL),
        ).fetchone()
        if row is not None and not row["stale"]:
            return
        rebuild_summary(
            self.db,
            user_id,
            scope=SCOPE_GLOBAL,
            max_tokens=self.config.memory_md_token_limit,
        )

    # -- mutation surface -----------------------------------------------------------

    async def replace(self, user_id: str, fact_id: str, new_text: str) -> dict:
        """Replace an active fact with a new statement.

        The old fact is soft-superseded (``status='superseded'`` with
        ``superseded_by`` pointing at the newest replacement fact) by the
        worker asynchronously.

        Args:
            user_id: The user who owns the fact.
            fact_id: The active fact to replace.
            new_text: The replacement utterance to extract into facts.

        Returns:
            A dict ``{"candidate_id", "status", "trace_id"}``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")

        old = self.db.execute(
            "SELECT session_id FROM facts WHERE user_id = ? AND fact_id = ? "
            "AND status = 'active'",
            (user_id, fact_id),
        ).fetchone()
        if old is None:
            raise ValueError(
                f"no active fact {fact_id} for user {user_id} to replace"
            )

        candidate_id = str(uuid.uuid4())
        created_at = now_ms()
        with self.db:
            self.db.execute(
                "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
                "turn_id, raw_text, status, created_at) "
                "VALUES (?, ?, ?, 0, ?, 'pending', ?)",
                (candidate_id, user_id, old["session_id"], new_text, created_at),
            )
            self.db.execute(
                "INSERT INTO task_queue(task_id, task_type, payload, status, "
                "priority, retry_count, max_retries, created_at) "
                "VALUES (?, 'replace', ?, 'pending', 5, 0, ?, ?)",
                (
                    str(uuid.uuid4()),
                    json.dumps(
                        {
                            "candidate_id": candidate_id,
                            "user_id": user_id,
                            "old_fact_id": fact_id,
                            "new_text": new_text,
                            "session_id": old["session_id"],
                            "turn_id": 0,
                        }
                    ),
                    self.config.max_retries,
                    created_at,
                ),
            )
        return {"candidate_id": candidate_id, "status": "pending", "trace_id": str(uuid.uuid4())}

    async def forget(
        self, user_id: str, fact_id: Optional[str] = None, session_id: Optional[str] = None
    ) -> dict:
        """Forget (soft-delete) a fact, or every active fact of a session.

        Targeted facts are marked ``status='retracted'`` by the worker
        asynchronously. Pass exactly one of ``fact_id`` or ``session_id``.

        Args:
            user_id: The user who owns the memory.
            fact_id: The specific fact to retract.
            session_id: Retract all active facts of this session instead.

        Returns:
            A dict ``{"candidate_id", "status", "trace_id"}``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        if not fact_id and not session_id:
            raise ValueError("forget requires either fact_id or session_id")

        candidate_id = str(uuid.uuid4())
        created_at = now_ms()
        with self.db:
            self.db.execute(
                "INSERT INTO fact_candidates(candidate_id, user_id, session_id, "
                "turn_id, raw_text, status, created_at) "
                "VALUES (?, ?, ?, 0, NULL, 'pending', ?)",
                (candidate_id, user_id, session_id or "", created_at),
            )
            self.db.execute(
                "INSERT INTO task_queue(task_id, task_type, payload, status, "
                "priority, retry_count, max_retries, created_at) "
                "VALUES (?, 'forget', ?, 'pending', 5, 0, ?, ?)",
                (
                    str(uuid.uuid4()),
                    json.dumps(
                        {
                            "candidate_id": candidate_id,
                            "user_id": user_id,
                            "fact_id": fact_id,
                            "session_id": session_id,
                        }
                    ),
                    self.config.max_retries,
                    created_at,
                ),
            )
        return {"candidate_id": candidate_id, "status": "pending", "trace_id": str(uuid.uuid4())}

    # --- UI-facing edit / backup / restore surface ---------------------------

    def list_facts(
        self,
        user_id: str,
        include_retracted: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Paginate a user's facts in descending recency for the settings UI.

        Args:
            user_id: The user whose facts are listed.
            include_retracted: Whether to include soft-deleted rows.
            limit: Maximum rows returned.
            offset: Row offset for paging.

        Returns:
            ``{"facts", "total", "offset", "limit"}`` where each fact carries
            ``fact_id`` / ``subject`` / ``predicate`` / ``object`` / ``type`` /
            ``content`` / ``confidence`` / ``importance`` / ``status`` /
            ``created_at``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        status_clause = "AND status = 'active'" if not include_retracted else ""
        total = self.db.execute(
            f"SELECT COUNT(*) AS n FROM facts WHERE user_id = ? {status_clause}",
            (user_id,),
        ).fetchone()["n"]
        rows = self.db.execute(
            f"SELECT fact_id, subject, predicate, object, type, content, "
            f"confidence, importance, status, created_at FROM facts "
            f"WHERE user_id = ? {status_clause} "
            f"ORDER BY created_at DESC LIMIT ? OFFSET ?",
            (user_id, limit, offset),
        ).fetchall()
        facts = [
            {
                "fact_id": r["fact_id"],
                "subject": r["subject"],
                "predicate": r["predicate"],
                "object": r["object"],
                "type": r["type"] or "semantic",
                "content": r["content"],
                "confidence": r["confidence"],
                "importance": r["importance"],
                "status": r["status"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]
        return {"facts": facts, "total": total, "offset": offset, "limit": limit}

    async def edit_fact(
        self,
        user_id: str,
        fact_id: str,
        subject: Optional[str] = None,
        predicate: Optional[str] = None,
        object: Optional[str] = None,
        content: Optional[str] = None,
        type: Optional[str] = None,
    ) -> dict:
        """Directly update an active fact (user-invoked UI edit).

        Fields given as ``None`` are left unchanged. Editing re-embeds the
        fact's searchable text so retrieval and FTS stay consistent, then marks
        summaries stale (read paths refresh them on demand). Returns the
        updated fact.

        Args:
            user_id: Owner of the fact.
            fact_id: The active fact to edit.
            subject / predicate / object: SPO triple fields to change.
            content: Knowledge body to set (use a sentinel to clear).
            type: Memory type to set.

        Returns:
            The updated fact dict, or raises ``ValueError`` if the fact is not
            an active row owned by ``user_id``.
        """
        if self.db is None or self.embedder is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        row = self.db.execute(
            "SELECT fact_id FROM facts WHERE user_id = ? AND fact_id = ? "
            "AND status = 'active'",
            (user_id, fact_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"no active fact {fact_id} for user {user_id}")

        # Apply ordered updates directly to the row.
        updates: dict = {}
        if subject is not None:
            updates["subject"] = str(subject).strip()
        if predicate is not None:
            updates["predicate"] = str(predicate).strip()
        if object is not None:
            updates["object"] = str(object).strip()
        if content is not None:
            updates["content"] = content or None
        if type is not None:
            updates["type"] = str(type).strip() or "semantic"

        if updates:
            assignments = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [user_id, fact_id]
            with self.db:
                self.db.execute(
                    f"UPDATE facts SET {assignments} "
                    f"WHERE user_id = ? AND fact_id = ? AND status = 'active'",
                    values,
                )
            await self._resync_fact_vectors(user_id, fact_id)
            self._mark_and_refresh(user_id)

        return self._fetch_fact(user_id, fact_id)

    def list_profile(self, user_id: str) -> dict:
        """List a user's profile rows for the settings UI.

        Args:
            user_id: Owner of the profile.

        Returns:
            ``{"profile": [...]}`` rows with ``section`` / ``key`` / ``value`` /
            ``source`` / ``privacy``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        rows = self.db.execute(
            "SELECT section, key, value, source, privacy FROM user_profile "
            "WHERE user_id = ? ORDER BY section, key",
            (user_id,),
        ).fetchall()
        return {
            "profile": [
                {
                    "section": r["section"],
                    "key": r["key"],
                    "value": r["value"],
                    "source": r["source"],
                    "privacy": r["privacy"],
                }
                for r in rows
            ]
        }

    def upsert_profile(
        self,
        user_id: str,
        section: str,
        key: str,
        value: str,
    ) -> dict:
        """Add or update one user-profile row (user-invoked UI edit).

        Rows written here are tagged with the most-authoritative ``user_explicit``
        source so they are never silently downgraded by later derived writes.

        Args:
            user_id: Owner of the profile.
            section: Profile section (predicate).
            key: Key within the section (use ``"value"`` for simple rows).
            value: The stored value.

        Returns:
            ``{"ok": True}``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        section = str(section).strip()
        key = str(key).strip()
        value = str(value).strip()
        if not section or not key:
            raise ValueError("profile section and key are required")
        conn = self.db
        conn.execute(
            "INSERT INTO user_profile(user_id, section, key, value, source, "
            "confidence, privacy, updated_at) VALUES (?, ?, ?, ?, "
            "'user_explicit', 0.9, 'private', ?) "
            "ON CONFLICT(user_id, section, key) DO UPDATE SET "
            "value = excluded.value, source = 'user_explicit', "
            "confidence = 0.9, updated_at = excluded.updated_at",
            (user_id, section, key, value, now_ms()),
        )
        conn.commit()
        return {"ok": True}

    def delete_profile(self, user_id: str, section: str, key: str) -> dict:
        """Delete one user-profile row.

        Args:
            user_id: Owner of the profile.
            section: Profile section.
            key: Key within the section.

        Returns:
            ``{"ok": True, "deleted": n}``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        cur = self.db.execute(
            "DELETE FROM user_profile WHERE user_id = ? AND section = ? AND key = ?",
            (user_id, section, key),
        )
        self.db.commit()
        return {"ok": True, "deleted": cur.rowcount}

    def backup(self, user_id: str) -> dict:
        """Export the user's memory as a portable JSON snapshot.

        Args:
            user_id: Owner of the memory.

        Returns:
            The serializable backup dict (see :mod:`atom_memory.backup`).
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        return export_memory(self.db, user_id)

    async def restore(self, user_id: str, payload: dict) -> dict:
        """Import a backup snapshot, replacing the user's current memory.

        Uses replace semantics (see :mod:`atom_memory.backup`): the user's live
        facts / profile are soft-cleared then the snapshot is written back.

        Args:
            user_id: The user whose memory is replaced.
            payload: A validated backup dict.

        Returns:
            ``{"facts_written", "profile_written"}``.
        """
        if self.db is None or self.embedder is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        payload = validate_backup(payload)
        # import_memory is async and keeps DB writes on this (loop) thread,
        # offloading only the embedding to worker threads.
        result = await import_memory(
            self.db,
            user_id,
            payload,
            self.embedder.embed_one,
        )
        self._mark_and_refresh(user_id)
        return result

    # --- edit helpers ---------------------------------------------------------

    def _fetch_fact(self, user_id: str, fact_id: str) -> dict:
        row = self.db.execute(
            "SELECT fact_id, subject, predicate, object, type, content, "
            "confidence, importance, status, created_at FROM facts "
            "WHERE user_id = ? AND fact_id = ?",
            (user_id, fact_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"no fact {fact_id} for user {user_id}")
        return {
            "fact_id": row["fact_id"],
            "subject": row["subject"],
            "predicate": row["predicate"],
            "object": row["object"],
            "type": row["type"] or "semantic",
            "content": row["content"],
            "confidence": row["confidence"],
            "importance": row["importance"],
            "status": row["status"],
            "created_at": row["created_at"],
        }

    async def _resync_fact_vectors(self, user_id: str, fact_id: str) -> None:
        """Re-derive FTS + vector entries for one fact after an edit.

        The embedding is CPU-bound model inference, so it runs in a worker
        thread exactly like the library's own write path.
        """
        row = self.db.execute(
            "SELECT subject, predicate, object, content FROM facts "
            "WHERE user_id = ? AND fact_id = ?",
            (user_id, fact_id),
        ).fetchone()
        if row is None:
            return
        searchable = (
            f"{row['subject']} {row['predicate']} {row['object']} "
            f"{(row['content'] or '')}"
        ).strip()
        embed_text = searchable or " "
        blob = await asyncio.to_thread(self.embedder.embed_one, embed_text)
        with self.db:
            self.db.execute("DELETE FROM facts_fts WHERE fact_id = ?", (fact_id,))
            self.db.execute(
                "DELETE FROM facts_vec WHERE fact_id = ?", (fact_id,)
            )
            self.db.execute(
                "INSERT INTO facts_fts(fact_id, text) VALUES (?, ?)",
                (fact_id, " ".join(segment_text(searchable))),
            )
            self.db.execute(
                "INSERT INTO facts_vec(fact_id, embedding) VALUES (?, ?)",
                (fact_id, blob),
            )

    def _mark_and_refresh(self, user_id: str) -> None:
        """Mark summaries stale and enqueue a debounced rebuild after an edit.

        Read paths (``memory_md`` / ``recall`` / ``summary``) refresh on demand
        via ``_ensure_summary``, so this keeps derived views consistent even
        before the worker rebuild lands.
        """
        if self._worker is not None:
            self._worker._after_mutation(user_id)
        else:
            mark_stale(self.db, user_id)

    def stats(self, user_id: str) -> dict:
        """Return aggregate counters for a user.

        Args:
            user_id: The user whose counts are reported.

        Returns:
            A dict with ``facts`` (active), ``pending`` (unprocessed
            candidates), ``stale_summaries`` and ``summaries`` (fresh).
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")

        facts = self.db.execute(
            "SELECT COUNT(*) AS n FROM facts WHERE user_id = ? AND status = 'active'",
            (user_id,),
        ).fetchone()
        pending = self.db.execute(
            "SELECT COUNT(*) AS n FROM fact_candidates "
            "WHERE user_id = ? AND status = 'pending'",
            (user_id,),
        ).fetchone()
        stale = self.db.execute(
            "SELECT COUNT(*) AS n FROM summaries WHERE user_id = ? AND stale = 1",
            (user_id,),
        ).fetchone()
        fresh = self.db.execute(
            "SELECT COUNT(*) AS n FROM summaries WHERE user_id = ? AND stale = 0",
            (user_id,),
        ).fetchone()

        return {
            "facts": facts["n"],
            "pending": pending["n"],
            "stale_summaries": stale["n"],
            "summaries": fresh["n"],
        }
