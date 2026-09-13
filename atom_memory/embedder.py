"""FastEmbed-backed embedding provider.

Wraps :class:`fastembed.TextEmbedding` so that the rest of the library sees a
stable, lazy interface that returns ``serialize_float32`` BLOBs (the exact
binary format ``sqlite-vec``'s ``vec0`` virtual table expects for
``float[...]`` columns).
"""

from __future__ import annotations

import logging
import struct
from typing import List, Optional, Sequence

logger = logging.getLogger(__name__)


def serialize_float32(values: Sequence[float]) -> bytes:
    """Serialize a sequence of floats to a little-endian float32 BLOB.

    This is the on-wire format expected by ``sqlite-vec``'s ``vec0`` for
    ``float[...]`` columns.

    Args:
        values: The floats to encode.

    Returns:
        A ``bytes`` object of ``len(values) * 4`` bytes (little-endian float32).
    """
    return struct.pack(f"<{len(values)}f", *values)


def deserialize_float32(blob: bytes) -> List[float]:
    """Deserialize a ``serialize_float32`` BLOB back into Python floats.

    Args:
        blob: The BLOB produced by :func:`serialize_float32`.

    Returns:
        A list of floats of length ``len(blob) // 4``.
    """
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


class Embedder:
    """Lazy FastEmbed wrapper producing ``serialize_float32`` BLOBs.

    The underlying model is initialised lazily on first use and is resolved
    **offline-first**: FastEmbed is asked for a locally-cached model before
    any network source is touched, so runtime embedding makes no network
    calls. When the model has not been cached yet and ``allow_download`` is
    enabled (first-run setup), a one-time download is attempted.

    Attributes:
        model_name: FastEmbed model identifier.
        dim: Expected embedding dimensionality.
        allow_download: Whether a network download may be attempted when the
            model is not present in the local cache.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-zh-v1.5",
        dim: int = 512,
        allow_download: bool = True,
    ) -> None:
        """Initialise the wrapper.

        Args:
            model_name: FastEmbed model name.
            dim: Expected embedding dimensionality.
            allow_download: Permit a one-time network download when the model
                is absent from the local cache. Defaults to ``True`` so a
                fresh install can fetch the model; once cached, all further
                loads are offline.
        """
        self.model_name = model_name
        self.dim = dim
        self.allow_download = allow_download
        self._embedder: Optional[object] = None

    def _ensure_loaded(self) -> object:
        """Lazily load the FastEmbed model.

        First tries the local cache (no network); only if the model is missing
        and ``allow_download`` is set does it fall back to an online fetch.

        Returns:
            The underlying FastEmbed ``TextEmbedding`` instance.

        Raises:
            ValueError: If the model can neither be found locally nor fetched
                (downloads disabled or network unavailable).
        """
        if self._embedder is not None:
            return self._embedder

        from fastembed import TextEmbedding

        # Offline-first: resolve from the local cache if available.
        try:
            logger.info("Loading embedding model %s (local cache)", self.model_name)
            self._embedder = TextEmbedding(
                model_name=self.model_name, local_files_only=True
            )
            return self._embedder
        except Exception as exc:
            if not self.allow_download:
                raise ValueError(
                    f"Embedding model {self.model_name} is not cached and "
                    "downloads are disabled"
                ) from exc
            # First-run: allow the one-time model download.
            logger.info(
                "Model %s not cached; downloading once (%s)",
                self.model_name, exc,
            )
            self._embedder = TextEmbedding(model_name=self.model_name)
            return self._embedder

    def embed(self, texts: Sequence[str]) -> List[bytes]:
        """Embed a sequence of texts into a list of float32 BLOBs.

        Args:
            texts: The text items to embed.

        Returns:
            A list of ``serialize_float32`` BLOBs, one per input text.
        """
        model = self._ensure_loaded()
        blobs: List[bytes] = []
        for vector in model.embed(list(texts)):
            coerced = [float(v) for v in vector]
            if len(coerced) != self.dim:
                raise ValueError(
                    f"Model produced {len(coerced)} dims, expected {self.dim}"
                )
            blobs.append(serialize_float32(coerced))
        return blobs

    def embed_one(self, text: str) -> bytes:
        """Embed a single text into one float32 BLOB.

        Args:
            text: The text to embed.

        Returns:
            A ``serialize_float32`` BLOB of exactly ``dim * 4`` bytes.
        """
        return self.embed([text])[0]
