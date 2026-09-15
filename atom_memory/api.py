"""Public ``AtomMem`` API — the in-process memory front door.

Exposes the full write/read/mutation surface:

    lifecycle  — start() / stop()
    write      — add(user, session, text)
    read       — recall(), summary(), user_md()
    mutate     — replace(), forget(), reinforce()
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
from .profile import derive_profile_from_facts, profile_md
from .reinforce import (
    KIND_USER_CONFIRMED,
    adjust,
    effective_importance_at,
    record_reinforcement,
)
from .retriever import Retriever, estimate_tokens, segment_text
from .summary import generate_summary
from .validator import is_multi_valued
from .worker import Worker

logger = logging.getLogger(__name__)


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
            A dict with keys ``facts``, ``pending``, ``conflicts``,
            ``token_count`` and ``trace_id``.
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
                    "effective_importance": fact.get(
                        "effective_importance", fact["importance"]
                    ),
                    # The decayed reuse count behind that score — never the raw
                    # snapshot column, which was taken at last_used_at.
                    "strength": fact.get("strength", 0.0),
                    "reinforce_count": float(fact.get("reinforce_count") or 0.0),
                    "last_used_at": fact.get("last_used_at"),
                    "final_score": fact["final_score"],
                    "status": fact["status"],
                }
            )
            used += t

        pending = (
            self._load_pending(user_id) if include_pending else []
        )

        return {
            "facts": facts,
            "pending": pending,
            "conflicts": self._load_conflicts(user_id),
            "token_count": used,
            "trace_id": str(uuid.uuid4()),
        }

    def _load_conflicts(self, user_id: str) -> list:
        """Report a user's *active* contradictory fact pairs, best-effort.

        A fact is in conflict when it and another active fact of the same user
        share a (subject, predicate) that is *single-valued* (see
        :func:`~atom_memory.validator.is_multi_valued`) yet hold different
        objects — exactly the condition the write-path validator rejects a new
        candidate for (:func:`~atom_memory.validator._check_conflict`). These
        normally arrive only through a legacy/imported row, since the write path
        blocks them; surfacing them here lets the model/user see that memory
        holds two values where it should hold one.

        Returns:
            A list of ``{"left": fact_id, "right": fact_id, "subject",
            "predicate", "object_left", "object_right"}`` — one entry per
            conflicting *pair*, each fact_id appearing on the left or the right
            (never both directions). Empty when memory holds no such pair.
        """
        if self.db is None:
            return []
        rows = self.db.execute(
            "SELECT fact_id, subject, predicate, object, type FROM facts "
            "WHERE user_id = ? AND status = 'active' ORDER BY created_at ASC",
            (user_id,),
        ).fetchall()
        # Group active facts by their single-valued (subject, predicate) key.
        groups: dict = {}
        for r in rows:
            memory_type = r["type"] or "semantic"
            if is_multi_valued(str(r["predicate"]), memory_type):
                continue
            groups.setdefault((r["subject"], r["predicate"]), []).append(r)

        conflicts: list = []
        for (subject, predicate), members in groups.items():
            # Distinct objects within one single-valued key -> contradiction.
            distinct: dict = {}
            for r in members:
                obj = r["object"]
                distinct.setdefault(obj, r["fact_id"])
            if len(distinct) < 2:
                continue
            ordered_ids = list(distinct.values())
            for i in range(len(ordered_ids)):
                for j in range(i + 1, len(ordered_ids)):
                    conflicts.append(
                        {
                            "left": ordered_ids[i],
                            "right": ordered_ids[j],
                            "subject": subject,
                            "predicate": predicate,
                            "object_left": list(distinct.keys())[i],
                            "object_right": list(distinct.keys())[j],
                        }
                    )
        return conflicts

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

    async def summary(
        self,
        user_id: str,
        max_tokens: int = 1500,
        detail: bool = True,
    ) -> str:
        """Render the user's ``summary`` derived view.

        Args:
            user_id: The user whose memory is rendered.
            max_tokens: Estimated token cap for the rendered text, footer
                included.
            detail: ``True`` (the default) lists every active fact with its
                ``fact_id``. ``False`` renders the compact, type-grouped digest
                the dsh host freezes into the session system prompt — no
                ``fact_id`` (the UUIDs cost more tokens than they carry
                information for the model) and priority-ordered rather than
                recency-ordered.

        Returns:
            A markdown string.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        return generate_summary(self.db, user_id, max_tokens, detail)

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
            ``content`` / ``confidence`` / ``importance`` /
            ``effective_importance`` / ``reinforce_count`` / ``last_used_at`` /
            ``status`` / ``created_at``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        status_clause = "AND status = 'active'" if not include_retracted else ""
        # Decay every fact's reinforcement snapshot to one shared instant, so the
        # page is internally consistent and matches what retrieval would rank.
        at = now_ms()
        total = self.db.execute(
            f"SELECT COUNT(*) AS n FROM facts WHERE user_id = ? {status_clause}",
            (user_id,),
        ).fetchone()["n"]
        rows = self.db.execute(
            f"SELECT fact_id, subject, predicate, object, type, content, "
            f"confidence, importance, status, created_at, reinforce_count, "
            f"last_used_at FROM facts "
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
                "effective_importance": round(
                    effective_importance_at(
                        float(r["importance"] or 0.0),
                        float(r["reinforce_count"] or 0.0),
                        r["last_used_at"],
                        at,
                    ),
                    6,
                ),
                "strength": round(
                    adjust(float(r["reinforce_count"] or 0.0), r["last_used_at"], at),
                    6,
                ),
                "reinforce_count": float(r["reinforce_count"] or 0.0),
                "last_used_at": r["last_used_at"],
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
        fact's searchable text so retrieval and FTS stay consistent. Returns the
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

        # Deliberately does *not* reinforce. An edit can be a reword, a type
        # fix, or a wholesale correction — none of which is evidence that the
        # fact was reused, and one of which ("fix a wrong memory") is evidence
        # against it. Treating every UI write as a confirmation let the settings
        # panel mint the strongest signal in the model (gain 1.0) for free.
        # Callers that genuinely mean "the user confirmed this" call
        # `reinforce(...)` themselves, which is explicit and auditable.
        return self._fetch_fact(user_id, fact_id)

    def reinforce(
        self,
        user_id: str,
        fact_id: str,
        kind: str = KIND_USER_CONFIRMED,
        session_id: str = "s_ui",
    ) -> dict:
        """Record a reuse event for a fact and return its new strength.

        This is the explicit half of the reinforcement loop; the implicit half
        fires automatically when the extractor observes the user re-stating a
        claim that is already stored (see :mod:`~atom_memory.reinforce`).

        Only genuine reuse should be reported here. In particular, do **not**
        call this for a mere retrieval hit: recall feeding back into the score
        is the self-reinforcing loop the design deliberately excludes.

        Args:
            user_id: Owner of the fact.
            fact_id: The active fact to strengthen.
            kind: Evidence kind — one of ``user_confirmed`` (default),
                ``user_restated``, ``applied``, ``retrieved_only``.
            session_id: Session the evidence came from; it scopes the
                idempotency guard, so one fact strengthens at most once per
                session per kind.

        Returns:
            A dict with ``fact_id``, ``kind``, ``reinforce_count`` (the stored
            snapshot), ``strength`` (the same snapshot decayed to now — what the
            ranker actually uses), ``importance`` (base),
            ``effective_importance`` (an alias of ``strength``), ``last_used_at``,
            ``gain`` and ``applied`` (``False`` for a suppressed duplicate).

        Raises:
            RuntimeError: If the memory is not started.
            ValueError: If the fact is not active or not owned by ``user_id``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        base_row = self.db.execute(
            "SELECT importance FROM facts "
            "WHERE user_id = ? AND fact_id = ? AND status = 'active'",
            (user_id, fact_id),
        ).fetchone()
        if base_row is None:
            raise ValueError(f"no active fact {fact_id} for user {user_id}")
        base = float(base_row["importance"] or 0.0)

        result = record_reinforcement(
            self.db, fact_id, user_id, session_id, kind
        )
        if result is None:
            # The kind is unregistered, or this session already contributed this
            # kind of evidence. Report the unchanged state.
            row = self.db.execute(
                "SELECT reinforce_count, last_used_at FROM facts "
                "WHERE fact_id = ?",
                (fact_id,),
            ).fetchone()
            snapshot = float(row["reinforce_count"] or 0.0)
            strength = effective_importance_at(
                base, snapshot, row["last_used_at"]
            )
            return {
                "fact_id": fact_id,
                "kind": kind,
                "reinforce_count": snapshot,
                "strength": round(strength, 6),
                "importance": base,
                "effective_importance": round(strength, 6),
                "last_used_at": row["last_used_at"],
                "gain": 0.0,
                "applied": False,
            }
        # ``result`` was computed at the event instant, which for the default
        # (now) path is this instant, so ``strong_after`` is already current.
        return {
            "fact_id": fact_id,
            "kind": kind,
            "reinforce_count": result.n,
            "strength": result.strong_after,
            "importance": base,
            "effective_importance": result.strong_after,
            "last_used_at": result.last_used_at,
            "gain": result.gain,
            "applied": result.applied,
        }

    def list_profile(self, user_id: str) -> dict:
        """List a user's profile rows for the settings UI.

        Args:
            user_id: Owner of the profile.

        Returns:
            ``{"profile": [...]}`` rows with ``section`` / ``key`` / ``value`` /
            ``source`` / ``privacy`` / ``pinned`` (whether the row is fixed
            against automatic memory updates).
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        rows = self.db.execute(
            "SELECT section, key, value, source, privacy, pinned FROM user_profile "
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
                    "pinned": bool(r["pinned"]),
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
        pinned: Optional[bool] = None,
    ) -> dict:
        """Add or update one user-profile row (user-invoked UI edit).

        Rows written here are tagged with the most-authoritative ``user_explicit``
        source so they are never silently downgraded by later derived writes.

        This is the one write path that may touch a **pinned** row: the pin's
        owner is the human in the panel, so editing a value here — or flipping
        the pin itself — has to work, or a pinned row could never be corrected
        or released. Every automatic path goes through
        :func:`atom_memory.profile.upsert_profile`, which refuses pinned rows.

        Args:
            user_id: Owner of the profile.
            section: Profile section (predicate).
            key: Key within the section (use ``"value"`` for simple rows).
            value: The stored value.
            pinned: New pin state; ``None`` keeps the row's current state (and
                means unpinned for a new row).

        Returns:
            ``{"ok": True, "pinned": bool}``.
        """
        if self.db is None:
            raise RuntimeError("AtomMem is not started; call start() first")
        section = str(section).strip()
        key = str(key).strip()
        value = str(value).strip()
        if not section or not key:
            raise ValueError("profile section and key are required")
        pinned_flag = None if pinned is None else (1 if pinned else 0)
        conn = self.db
        conn.execute(
            "INSERT INTO user_profile(user_id, section, key, value, source, "
            "confidence, privacy, pinned, updated_at) VALUES (?, ?, ?, ?, "
            "'user_explicit', 0.9, 'private', COALESCE(?, 0), ?) "
            "ON CONFLICT(user_id, section, key) DO UPDATE SET "
            "value = excluded.value, source = 'user_explicit', "
            "confidence = 0.9, pinned = COALESCE(?, pinned), "
            "updated_at = excluded.updated_at",
            (user_id, section, key, value, pinned_flag, now_ms(), pinned_flag),
        )
        conn.commit()
        row = conn.execute(
            "SELECT pinned FROM user_profile "
            "WHERE user_id = ? AND section = ? AND key = ?",
            (user_id, section, key),
        ).fetchone()
        return {"ok": True, "pinned": bool(row["pinned"]) if row else False}

    def delete_profile(self, user_id: str, section: str, key: str) -> dict:
        """Delete one user-profile row.

        Deletion is an explicit act only: nothing in the memory pipeline deletes
        profile rows, so a pinned row is deleted like any other when the user
        asks for it (the pin freezes automatic *updates*, not the user's own
        editorial actions).

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
        return result

    # --- edit helpers ---------------------------------------------------------

    def _fetch_fact(self, user_id: str, fact_id: str) -> dict:
        row = self.db.execute(
            "SELECT fact_id, subject, predicate, object, type, content, "
            "confidence, importance, status, created_at, reinforce_count, "
            "last_used_at FROM facts "
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
            "effective_importance": round(
                effective_importance_at(
                    float(row["importance"] or 0.0),
                    float(row["reinforce_count"] or 0.0),
                    row["last_used_at"],
                ),
                6,
            ),
            "strength": round(
                adjust(float(row["reinforce_count"] or 0.0), row["last_used_at"]), 6
            ),
            "reinforce_count": float(row["reinforce_count"] or 0.0),
            "last_used_at": row["last_used_at"],
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

    def stats(self, user_id: str) -> dict:
        """Return aggregate counters for a user.

        Args:
            user_id: The user whose counts are reported.

        Returns:
            A dict with ``facts`` (active) and ``pending`` (unprocessed
            candidates).
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

        return {
            "facts": facts["n"],
            "pending": pending["n"],
        }
