"""Retrieval pipeline: FTS5 + vector KNN + RRF fusion + re-ranking.

Implements the recall half of the memory loop (spec 8.2 / 8.3):

    retrieval  = FTS5 (lexical, jieba-segmented query) ⊕ vec0 (semantic KNN)
    fusion     = Reciprocal Rank Fusion (RRF)
    re-rank    = 0.4·rrf_norm + 0.2·importance_norm + 0.2·recency_norm
                 + 0.2·trust_score
    trust      = 0.6·confidence + 0.4·source_credibility

All lookups are hard-scoped to ``user_id`` and only ``active`` facts are
considered.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from typing import Callable, Dict, List, Optional, Sequence

from .db import now_ms

logger = logging.getLogger(__name__)

# Source-of-truth credibility scores (spec 8.3).
SOURCE_CREDIBILITY: Dict[str, float] = {
    "user_explicit": 1.00,
    "user_confirmed": 0.95,
    "system_inferred_high": 0.80,
    "external_tool": 0.70,
    "indirect_inferred": 0.60,
    "system_inferred_low": 0.50,
    "model_generated": 0.30,
}

# Weights for the re-ranking formula (spec 8.3).
W_RRF = 0.4
W_IMPORTANCE = 0.2
W_RECENCY = 0.2
W_TRUST = 0.2

T_TRUST_CONFIDENCE = 0.6
T_TRUST_SOURCE = 0.4


def rrf_merge(
    fts: Sequence[str], vec: Sequence[str], k: int = 60
) -> List[tuple]:
    """Fuse the FTS and vector result lists by Reciprocal Rank Fusion.

    Args:
        fts: Ordered list of fact_ids from FTS (best first).
        vec: Ordered list of fact_ids from vector KNN (best first).
        k: RRF constant.

    Returns:
        A list of ``(fact_id, rrf_score)`` sorted descending by score.
    """
    scores: Dict[str, float] = {}
    for rank, fact_id in enumerate(fts):
        scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, fact_id in enumerate(vec):
        scores[fact_id] = scores.get(fact_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


class Retriever:
    """Ranked recall of active atomic facts for a user."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        embed_one: Callable[[str], bytes],
        top_k_default: int = 10,
    ) -> None:
        """Initialise the retriever.

        Args:
            conn: The SQLite connection.
            embed_one: Callable mapping text to a serialized embedding BLOB.
            top_k_default: Default number of candidates to return.
        """
        self.conn = conn
        self.embed_one = embed_one
        self.top_k_default = top_k_default

    async def search(self, user_id: str, query: str, top_k: Optional[int] = None) -> List[dict]:
        """Run the full retrieval pipeline and return ranked facts.

        Args:
            user_id: The user whose memory is searched (hard isolation scope).
            query: The natural-language query.
            top_k: Maximum number of facts to return (defaults to
                ``self.top_k_default``).

        Returns:
            A list of fact dicts ordered by descending ``final_score``, each
            with keys ``fact_id``, ``subject``, ``predicate``, ``object``,
            ``confidence``, ``importance``, ``source_type``, ``status``,
            ``created_at`` and ``final_score``.
        """
        k = top_k or self.top_k_default
        query = (query or "").strip()
        if not query:
            return []

        blob = await asyncio.to_thread(self.embed_one, query)

        vec_ids = self._vector_knn(user_id, blob, k)
        fts_ids = self._fts_search(user_id, query, k)

        fused = rrf_merge(fts_ids, vec_ids)
        if not fused:
            return []

        # Pull full fact rows for the fused ids.
        facts = self._fetch_facts(user_id, [fid for fid, _ in fused])

        # RRF scores in the same order as fused.
        rrf_scores = dict(fused)
        ranked = self._rerank(facts, rrf_scores)
        return ranked[:k]

    # -- retrieval primitives -------------------------------------------------

    def _vector_knn(self, user_id: str, blob: bytes, k: int) -> List[str]:
        """Return fact_ids from semantic KNN, best first.

        Restricts candidates to the user's active facts via an ``IN``
        subquery; sqlite-vec requires the KNN form with an explicit ``LIMIT``.
        """
        try:
            rows = self.conn.execute(
                "SELECT fact_id FROM facts_vec WHERE embedding MATCH ? "
                "AND fact_id IN (SELECT fact_id FROM facts "
                "WHERE user_id = ? AND status = 'active') "
                "ORDER BY distance LIMIT ?",
                (blob, user_id, k),
            ).fetchall()
            return [r["fact_id"] for r in rows]
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.warning("vector KNN failed: %s", exc)
            return []

    def _fts_search(self, user_id: str, query: str, k: int) -> List[str]:
        """Return fact_ids matching a jieba-segmented FTS query, best first.

        FTS is best-effort: malformed queries or empty token streams are
        swallowed and return nothing rather than breaking retrieval.
        """
        tokens = segment_text(query)
        if not tokens:
            return []
        match = " OR ".join(f'"{t}"' for t in tokens)
        try:
            rows = self.conn.execute(
                "SELECT f.fact_id FROM facts_fts fts "
                "JOIN facts f ON f.fact_id = fts.fact_id "
                "WHERE f.user_id = ? AND f.status = 'active' "
                "AND facts_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (user_id, match, k),
            ).fetchall()
            return [r["fact_id"] for r in rows]
        except sqlite3.Error as exc:
            logger.debug("FTS search failed (%s): %s", match, exc)
            return []

    def _fetch_facts(
        self, user_id: str, fact_ids: Sequence[str]
    ) -> List[dict]:
        """Fetch full fact rows for the given ids (user-scoped)."""
        if not fact_ids:
            return []
        placeholders = ",".join("?" for _ in fact_ids)
        rows = self.conn.execute(
            f"SELECT fact_id, subject, predicate, object, confidence, "
            f"importance, source_type, status, created_at, type, content "
            f"FROM facts WHERE user_id = ? AND fact_id IN ({placeholders})",
            [user_id, *fact_ids],
        ).fetchall()
        by_id = {r["fact_id"]: dict(r) for r in rows}
        # Preserve the fused order.
        return [by_id[fid] for fid in fact_ids if fid in by_id]

    # -- ranking --------------------------------------------------------------

    def _rerank(
        self, facts: List[dict], rrf_scores: Dict[str, float]
    ) -> List[dict]:
        """Apply the spec 8.3 weighting formula and sort by final_score."""
        if not facts:
            return []

        rrf_vals = [rrf_scores.get(f["fact_id"], 0.0) for f in facts]
        imp_vals = [float(f["importance"]) for f in facts]
        age_vals = [now_ms() - int(f["created_at"] or 0) for f in facts]

        rrf_norm = _minmax(rrf_vals)
        imp_norm = _minmax(imp_vals)
        recency_norm = _minmax([-a for a in age_vals])  # older -> smaller

        ranked: List[dict] = []
        for i, fact in enumerate(facts):
            trust = (
                T_TRUST_CONFIDENCE * float(fact["confidence"])
                + T_TRUST_SOURCE
                * SOURCE_CREDIBILITY.get(fact["source_type"], 0.5)
            )
            final = (
                W_RRF * rrf_norm[i]
                + W_IMPORTANCE * imp_norm[i]
                + W_RECENCY * recency_norm[i]
                + W_TRUST * trust
            )
            item = dict(fact)
            item["final_score"] = round(final, 4)
            ranked.append(item)

        ranked.sort(key=lambda x: x["final_score"], reverse=True)
        return ranked


# -- helpers ------------------------------------------------------------------


def _minmax(values: Sequence[float]) -> List[float]:
    """Min-max normalize a sequence into [0, 1]; a constant series maps to 1."""
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0 for _ in values]
    span = hi - lo
    return [(v - lo) / span for v in values]


def estimate_tokens(text: str) -> int:
    """Estimate the number of tokens in a text for budget trimming.

    A simple, deterministic heuristic: CJK characters count as one token each,
    and every five non-CJK characters count as one word token. Not an exact
    tokenizer — just a stable proxy for budgeting.

    Args:
        text: The text to estimate.

    Returns:
        A non-negative integer token estimate.
    """
    if not text or not text.strip():
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    word_tokens = (other // 5) if other else 0
    return cjk + max(word_tokens, 1 if other else 0)


def segment_text(text: str) -> List[str]:
    """Segment Chinese text into distinct, FTS-safe tokens.

    Falls back to the raw text split on whitespace if jieba is unavailable.
    Used both for query matching and (via the worker) for FTS indexing.

    Args:
        text: The text to segment.

    Returns:
        A list of distinct non-empty token strings.
    """
    try:
        import jieba

        jieba.setLogLevel(60)  # silence jieba's init banner
        tokens = [t for t in jieba.cut(text) if t and t.strip()]
    except Exception:  # pragma: no cover - jieba is a declared dependency
        tokens = [t for t in text.replace(" ", "").split() if t]
    seen = set()
    out = []
    for t in tokens:
        t = t.strip().replace('"', "").replace("'", "")
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out
