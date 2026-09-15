"""dsh-atom-memory: lightweight in-process long-term memory for DeepSeek Harness.

The public entry point is :class:`~atom_memory.api.AtomMem`, which wraps
the full pipeline: write (add/extract/validate), recall (FTS + vector), soft
replace/forget and derived views (summary, user profile).
"""

from __future__ import annotations

from .api import AtomMem
from .config import MemConfig
from .db import open_db
from .embedder import Embedder, deserialize_float32, serialize_float32
from .models import (
    PRED_EVENT,
    TYPE_DECISION_RULE,
    TYPE_EPISODIC,
    TYPE_FEW_SHOT,
    TYPE_LESSON,
    TYPE_PROCEDURAL,
    TYPE_SEMANTIC,
    TYPE_SOP,
    AtomicFact,
    FactCandidate,
    ValidationResult,
)
from .reinforce import (
    KIND_APPLIED,
    KIND_RETRIEVED_ONLY,
    KIND_USER_CONFIRMED,
    KIND_USER_RESTATED,
    effective_importance,
    reinforce_bonus,
)

__all__ = [
    "AtomMem",
    "MemConfig",
    "open_db",
    "Embedder",
    "serialize_float32",
    "deserialize_float32",
    "FactCandidate",
    "AtomicFact",
    "ValidationResult",
    "TYPE_SEMANTIC",
    "TYPE_PROCEDURAL",
    "TYPE_EPISODIC",
    "TYPE_SOP",
    "TYPE_DECISION_RULE",
    "TYPE_FEW_SHOT",
    "TYPE_LESSON",
    "PRED_EVENT",
    "KIND_USER_CONFIRMED",
    "KIND_USER_RESTATED",
    "KIND_APPLIED",
    "KIND_RETRIEVED_ONLY",
    "reinforce_bonus",
    "effective_importance",
]

__version__ = "0.1.0"

