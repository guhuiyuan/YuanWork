"""Reranker module - bge-reranker-v2-m3 cross-encoder for top-K refinement.

Stage 2 enhancement: HybridRetriever's first-stage vector recall fetches
top_k_initial (default 50) candidates, then a cross-encoder reranker
re-scores them and keeps the top top_k_rerank (default 10).

The model is large (~568MB, ~1.2GB RAM) so it's loaded lazily on first
use and can be disabled via the `use_reranker` config flag.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from yuanwork.utils.model_load import load_model_offline_first

logger = logging.getLogger(__name__)


def _import_flag_reranker():
    from FlagEmbedding import FlagReranker

    return FlagReranker


class Reranker:
    """Cross-encoder reranker backed by bge-reranker-v2-m3."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        device: str = "cpu",
        use_fp16: bool = False,
    ):
        self.model_name = model_name
        self.device = device
        self.use_fp16 = use_fp16
        self._model: Any | None = None
        self._load_lock = asyncio.Lock()

    def is_loaded(self) -> bool:
        return self._model is not None

    def load(self) -> None:
        """Synchronously load the model. Idempotent.

        Heavy operation: ~5-30s on CPU for first run, depends on disk speed.
        Call from `asyncio.to_thread` if you need non-blocking behavior.
        """
        if self._model is not None:
            return

        FlagReranker = _import_flag_reranker()

        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
        logger.info(f"Loading reranker {self.model_name} on {self.device}...")
        # From the local cache first: the model is there, and asking
        # the Hub anyway costs a hanging connection (ADR-045).
        self._model = load_model_offline_first(
            FlagReranker,
            self.model_name,
            use_fp16=self.use_fp16,
            device=self.device,
        )
        logger.info("Reranker loaded")

    async def load_async(self) -> None:
        """Async-safe loader. Multiple concurrent callers serialize on the lock."""
        if self._model is not None:
            return
        async with self._load_lock:
            if self._model is not None:
                return
            await asyncio.to_thread(self.load)

    async def rerank(
        self,
        query: str,
        chunks: list[dict[str, Any]],
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """Rerank chunks by cross-encoder score (descending).

        Each input chunk dict is augmented with `rerank_score`. The output
        is sorted by that score. If `top_k` is given, the list is truncated
        to that length.
        """
        if not chunks:
            return chunks
        await self.load_async()

        # Build (query, text) pairs. Use the chunk's `text` field; fallback
        # to the citation `text` for objects that are already-shaped.
        pairs: list[tuple[str, str]] = []
        for c in chunks:
            text = c.get("text") or c.get("citation_text") or ""
            pairs.append((query, text))

        try:
            scores = await asyncio.to_thread(
                self._model.compute_score, pairs, normalize=True
            )
        except Exception as e:
            logger.warning(f"Reranker.compute_score failed: {e}; returning input order")
            return chunks[:top_k] if top_k else chunks

        # Pair scores with chunks and sort descending
        scored = list(zip(chunks, scores))
        scored.sort(key=lambda x: x[1], reverse=True)

        reranked: list[dict[str, Any]] = []
        for chunk, score in scored:
            # Copy so we don't mutate caller's dicts
            new_chunk = dict(chunk)
            new_chunk["rerank_score"] = float(score)
            reranked.append(new_chunk)

        if top_k is not None:
            reranked = reranked[:top_k]
        return reranked
