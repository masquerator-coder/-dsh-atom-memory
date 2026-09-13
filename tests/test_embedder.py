"""Tests for the embedding layer (embedder.py)."""

from __future__ import annotations

import os

import pytest

from dsh_atom_memory.embedder import (
    Embedder,
    deserialize_float32,
    serialize_float32,
)


def test_serialize_roundtrip():
    """serialize_float32 -> deserialize_float32 must recover the values."""
    values = [0.0, 1.0, -1.5, 3.25, 0.5]
    blob = serialize_float32(values)
    assert isinstance(blob, bytes)
    assert len(blob) == len(values) * 4
    out = deserialize_float32(blob)
    assert out == pytest.approx(values)


def test_serialized_blob_sqlite_dtype():
    """The BLOB must be exactly 4 bytes per float (float32 width)."""
    blob = serialize_float32([1.0, 2.0, 3.0])
    assert len(blob) == 12


def test_deserialize_empty():
    """Deserializing an empty blob yields an empty list."""
    assert deserialize_float32(b"") == []


class _FakeVector:
    """Minimal stand-in that mimics a fastembed model's embed() yields."""

    def __init__(self, dim: int = 512) -> None:
        self.dim = dim

    def embed(self, texts):
        for t in texts:
            yield [1.0] * self.dim


def test_embed_one_dimension_with_fake_model(monkeypatch):
    """Embedder must express exactly ``dim`` floats and sized blob."""
    embedder = Embedder(dim=512)
    monkeypatch.setattr(embedder, "_ensure_loaded", lambda: _FakeVector(512))
    blob = embedder.embed_one("你好")
    assert len(blob) == 512 * 4
    floats = deserialize_float32(blob)
    assert len(floats) == 512
    assert all(v == 1.0 for v in floats)


def test_embed_dimension_mismatch_raises(monkeypatch):
    """A model producing an unexpected dimension must raise ValueError."""
    embedder = Embedder(dim=512)
    monkeypatch.setattr(embedder, "_ensure_loaded", lambda: _FakeVector(64))
    with pytest.raises(ValueError):
        embedder.embed_one("hello")


def test_embed_multi_returns_per_text_blob(monkeypatch):
    """embed() must return one blob per input text."""
    embedder = Embedder(dim=512)
    monkeypatch.setattr(embedder, "_ensure_loaded", lambda: _FakeVector(512))
    blobs = embedder.embed(["a", "b", "c"])
    assert len(blobs) == 3
    assert all(len(b) == 512 * 4 for b in blobs)


def test_serialize_float32_matches_struct_layout():
    """Float32 layout must match struct '<f' (little-endian IEEE 754)."""
    import struct

    values = [0.1, 0.2, 0.3]
    assert serialize_float32(values) == struct.pack("<3f", *values)


@pytest.mark.skipif(
    not os.environ.get("DSH_ATOM_MEMORY_REAL_EMBED"),
    reason="Requires DSH_ATOM_MEMORY_REAL_EMBED=1 (downloads the FastEmbed model over the network).",
)
def test_real_model_512_dim_on_explicit_request():
    """Real-model smoke test, only when the operator opts in explicitly."""
    embedder = Embedder()
    blob = embedder.embed_one("你好")
    assert len(blob) == 512 * 4
    floats = deserialize_float32(blob)
    assert len(floats) == 512
    assert any(v != 0.0 for v in floats)  # not a zero-vector
