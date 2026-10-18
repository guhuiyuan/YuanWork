"""Semantic Memory - LanceDB vector store for document chunks.

Uses bge-m3 for embedding (dense + sparse vectors).
Supports hybrid retrieval (vector + BM25 + metadata filter).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


# Lazy imports for heavy deps
def _import_lancedb():
    import lancedb

    return lancedb


def _import_flag_embedding():
    from FlagEmbedding import BGEM3FlagModel

    return BGEM3FlagModel


class SemanticMemory:
    """LanceDB-backed semantic memory store."""

    CHUNKS_TABLE = "chunks"

    def __init__(
        self,
        data_dir: Path | str,
        embedding_model: str = "BAAI/bge-m3",
        embedding_device: str = "cpu",
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.embedding_model_name = embedding_model
        self.embedding_device = embedding_device
        self._db = None
        self._table = None
        self._embedder = None
        self._embedding_dim: int | None = None
        # Set to True while a background prewarm is in flight so callers
        # (e.g. /health) can report "loading" instead of pretending the
        # model is ready.
        self._prewarm_in_flight: bool = False
        self._prewarm_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        """Initialize LanceDB connection."""
        lancedb = _import_lancedb()
        self._db = lancedb.connect(str(self.data_dir))
        logger.info(f"LanceDB connected at {self.data_dir}")

    async def close(self) -> None:
        """Close the database (LanceDB doesn't need explicit close but be safe)."""
        self._db = None
        self._table = None

    def is_embedding_loaded(self) -> bool:
        """Check if the embedding model is loaded."""
        return self._embedder is not None

    def load_embedding_model(self) -> None:
        """Load the embedding model synchronously (called from a thread).

        Returns immediately if already loaded. This is a CPU-bound operation
        that takes 1-3 minutes on first run; callers should use
        `load_embedding_model_async` to keep the event loop responsive.
        """
        if self._embedder is not None:
            return

        BGEM3FlagModel = _import_flag_embedding()

        # Suppress noisy output from FlagEmbedding
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

        logger.info(f"Loading embedding model {self.embedding_model_name} on {self.embedding_device}...")
        self._embedder = BGEM3FlagModel(
            self.embedding_model_name,
            devices=self.embedding_device,
        )
        logger.info("Embedding model loaded")

    async def load_embedding_model_async(self) -> None:
        """Async wrapper that runs the synchronous load in a thread."""
        if self._embedder is not None:
            return
        self._prewarm_in_flight = True
        try:
            await asyncio.to_thread(self.load_embedding_model)
        finally:
            self._prewarm_in_flight = False

    async def prewarm_embedding_model(self) -> None:
        """Kick off a background task to load the model without blocking.

        Idempotent: subsequent calls are no-ops. The health endpoint reports
        `_prewarm_in_flight` so the UI can show a "loading" state.
        """
        if self._embedder is not None or self._prewarm_in_flight:
            return
        self._prewarm_task = asyncio.create_task(self.load_embedding_model_async())

    def list_tables(self) -> list[str]:
        """List all tables in the database."""
        if self._db is None:
            return []
        return list(self._db.table_names())

    def get_or_create_chunks_table(self):
        """Get or create the chunks table.

        Note: `list_tables()` can be racy with `create_table()` on some
        lancedb versions, so we try `open_table` first and only fall
        back to `create_table` when the table is genuinely missing.
        """
        if self._db is None:
            raise RuntimeError("SemanticMemory not initialized")

        if self._table is not None:
            return self._table

        # Cheap path: already exists
        try:
            if self.CHUNKS_TABLE in self._db.list_tables():
                self._table = self._db.open_table(self.CHUNKS_TABLE)
                return self._table
        except Exception as e:
            logger.debug(f"list_tables() check failed: {e}")

        # Try opening directly (handles stale list_tables cache)
        try:
            self._table = self._db.open_table(self.CHUNKS_TABLE)
            return self._table
        except Exception:
            pass

        # Create from scratch
        import pyarrow as pa

        schema = pa.schema(
            [
                ("id", pa.string()),
                ("doc_id", pa.string()),
                ("chunk_idx", pa.int32()),
                ("text", pa.string()),
                ("vector", pa.list_(pa.float32(), 1024)),
                ("file_name", pa.string()),
                ("file_path", pa.string()),
                ("file_type", pa.string()),
                ("page", pa.int32()),
                ("slide_number", pa.int32()),
                ("section", pa.string()),
                ("content_type", pa.string()),
                ("token_count", pa.int32()),
                ("modified_at", pa.string()),
                ("created_at", pa.string()),
            ]
        )
        try:
            self._table = self._db.create_table(
                self.CHUNKS_TABLE, schema=schema, mode="create"
            )
        except ValueError as e:
            # Race: another worker created the table. Open it.
            if "already exists" in str(e).lower():
                self._table = self._db.open_table(self.CHUNKS_TABLE)
            else:
                raise
        return self._table

    async def embed_texts(
        self, texts: list[str], batch_size: int = 8
    ) -> list[list[float]]:
        """Embed texts using bge-m3. Returns dense vectors (1024-dim).

        The first call lazily loads the model in a thread, so the event
        loop stays responsive. Subsequent calls hit the loaded model
        directly.
        """
        if self._embedder is None:
            await self.load_embedding_model_async()

        # bge-m3 supports batched encoding. The encode call is CPU-bound
        # and takes a while; run it in a thread so other coroutines (like
        # health checks) can keep running.
        all_embeddings: list[list[float]] = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            result = await asyncio.to_thread(
                self._embedder.encode,
                batch,
                return_dense=True,
                return_sparse=False,
                return_colbert_vecs=False,
            )
            dense = result["dense_vecs"]
            if hasattr(dense, "tolist"):
                dense = dense.tolist()
            all_embeddings.extend(dense)
        return all_embeddings

    async def add_chunks(self, chunks: list[dict[str, Any]]) -> list[str]:
        """Add chunks to the vector store.

        Each chunk dict must contain:
        - id: str (unique chunk ID)
        - doc_id: str
        - chunk_idx: int
        - text: str
        And may contain metadata: file_name, file_path, page, slide_number, etc.

        Returns list of added chunk IDs.
        """
        if not chunks:
            return []

        table = self.get_or_create_chunks_table()

        # Embed all texts
        texts = [c["text"] for c in chunks]
        embeddings = await self.embed_texts(texts)

        from datetime import datetime

        now = datetime.now().isoformat()

        records = []
        for chunk, embedding in zip(chunks, embeddings):
            # Normalize vector to fixed length (pad/truncate to 1024)
            vec = list(embedding)[:1024]
            while len(vec) < 1024:
                vec.append(0.0)

            record = {
                "id": chunk["id"],
                "doc_id": chunk["doc_id"],
                "chunk_idx": chunk.get("chunk_idx", 0),
                "text": chunk["text"],
                "vector": vec,
                "file_name": chunk.get("file_name", ""),
                "file_path": chunk.get("file_path", ""),
                "file_type": chunk.get("file_type", "unknown"),
                "page": chunk.get("page"),
                "slide_number": chunk.get("slide_number"),
                "section": chunk.get("section"),
                "content_type": chunk.get("content_type", "text"),
                "token_count": chunk.get("token_count", len(chunk["text"]) // 4),
                "modified_at": chunk.get("modified_at", now),
                "created_at": now,
            }
            records.append(record)

        # Delete existing chunks with same IDs (for re-ingestion)
        existing_ids = [c["id"] for c in chunks]
        try:
            table.delete(f"id IN ({','.join(repr(i) for i in existing_ids)})")
        except Exception:
            pass  # Table might be empty

        # Add new records
        table.add(records)

        logger.info(f"Added {len(records)} chunks to LanceDB")
        return [c["id"] for c in chunks]

    async def search(
        self,
        query: str,
        top_k: int = 10,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """Vector similarity search.

        Args:
            query: Search query
            top_k: Number of results
            filter_expr: Optional LanceDB filter expression (e.g. "doc_id = 'xxx'")

        Returns:
            List of chunk dicts with scores
        """
        table = self.get_or_create_chunks_table()

        # Short-circuit when the KB is empty. This avoids triggering the
        # heavy bge-m3 model load on a fresh install.
        if table.count_rows() == 0:
            return []

        # Embed query
        embeddings = await self.embed_texts([query])
        query_vec = embeddings[0][:1024]
        while len(query_vec) < 1024:
            query_vec.append(0.0)

        # Search
        if filter_expr:
            results = table.search(query_vec).where(filter_expr).limit(top_k).to_list()
        else:
            results = table.search(query_vec).limit(top_k).to_list()

        # Extract distance (LanceDB returns _distance; smaller = closer)
        for r in results:
            distance = r.pop("_distance", 0.0)
            # Convert to similarity (1 / (1 + distance))
            r["_score"] = 1.0 / (1.0 + distance)

        return results

    async def delete_by_doc_id(self, doc_id: str) -> int:
        """Delete all chunks for a document."""
        table = self.get_or_create_chunks_table()
        try:
            table.delete(f"doc_id = '{doc_id}'")
            return 1  # LanceDB doesn't return count
        except Exception as e:
            logger.warning(f"Failed to delete chunks for doc {doc_id}: {e}")
            return 0

    async def count(self) -> int:
        """Count total chunks."""
        try:
            table = self.get_or_create_chunks_table()
            return table.count_rows()
        except Exception:
            return 0
