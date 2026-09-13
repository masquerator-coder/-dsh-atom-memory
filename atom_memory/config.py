"""Configuration dataclass for :mod:`atom_memory`.

The ``MemConfig`` dataclass centralises every tunable that the library needs,
from the SQLite file location to the embedding model name and the optional
LLM-based extractor callable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class MemConfig:
    """Runtime configuration for an :class:`~atom_memory.api.AtomMem` instance.

    Attributes:
        db_path: Filesystem location of the SQLite database file. A leading
            ``~`` is expanded to the current user's home directory. Parent
            directories are created automatically when the database is opened.
        embedding_model: The FastEmbed model name used to produce embeddings.
            Only the local ``BAAI/bge-small-zh-v1.5`` (512-dim) is supported by
            the schema; selecting another model requires a matching
            ``embedding_dim``.
        embedding_dim: Dimensionality of the embedding vectors written to the
            ``facts_vec`` virtual table. Must match the declared
            ``float[...]`` width and the chosen model.
        default_token_budget: Default token budget used by :meth:`recall`
            when the caller does not supply an explicit budget.
        memory_md_token_limit: Default token cap for the generated
            memory markdown.
        user_md_token_limit: Default token cap for the generated user-profile
            markdown.
        candidate_retention_days: Number of days an unresolved fact candidate
            is kept before it may be garbage collected.
        summary_rebuild_debounce_sec: Minimum seconds that must elapse between
            marking a summary stale and rebuilding it.
        max_retries: Maximum number of times the worker retries a failed task.
        worker_poll_interval_sec: Interval (seconds) at which the worker polls
            the task queue.
        llm_extractor: Optional callable used by the extractor to obtain fact
            candidates. It runs **first**; when it returns one or more usable
            candidates they are authoritative, and rule-based extraction only
            runs as a fallback (LLM threw or returned nothing). If ``None``,
            only rule-based extraction runs.
        privacy_filter: Default privacy tag applied to facts that do not
            declare one.
    """

    db_path: str = "~/.atom_memory/memory.db"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    default_token_budget: int = 2000
    memory_md_token_limit: int = 1500
    user_md_token_limit: int = 800
    candidate_retention_days: int = 7
    summary_rebuild_debounce_sec: int = 30
    max_retries: int = 3
    worker_poll_interval_sec: float = 0.5
    llm_extractor: Optional[Callable[..., list]] = field(default=None)
    privacy_filter: str = "private"

    def resolved_db_path(self) -> str:
        """Return ``db_path`` with ``~`` expanded to the home directory.

        Returns:
            An absolute path string suitable for :func:`os.path.expanduser`
            and friends.
        """
        return str(Path(self.db_path).expanduser())

