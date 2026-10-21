"""Semantic Memory - LanceDB vector store for document chunks.

Uses bge-m3 for embedding. Stage 3 added a bge-m3 **sparse** (lexical
weights) channel for fusion. Stage 3+ replaces the bge-m3 sparse
channel with a proper tantivy + jieba **FTS index** because bge-m3's
sparse scored 0.0 on pure numbers and short CJK tokens (verified on
the OCR test fixture). The bge-m3 sparse column is still populated
for back-compat / future reactivation; the `sparse_str` JSON column
is now informational only.

Retrieval modes:
  - `search(query, ...)`           — pure dense (back-compat)
  - `search_hybrid(query, ...)`    — dense (LanceDB) + sparse (tantivy FTS)
                                     fused via weighted sum (alpha/beta)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np

from yuanwork.utils.chunk_meta import chunk_meta

logger = logging.getLogger(__name__)


# Lazy imports for heavy deps
def _import_lancedb():
    import lancedb

    return lancedb


def _import_flag_embedding():
    from FlagEmbedding import BGEM3FlagModel

    return BGEM3FlagModel


# Schema versioning: when the chunks table schema evolves, bump this and
# add a migration block in `get_or_create_chunks_table`. v1 = initial.
# v2 = added `sparse_str` column for bge-m3 lexical weights.
CHUNKS_SCHEMA_VERSION = 2


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
        # Toggle: when True, embed_texts also asks bge-m3 for sparse
        # vectors. Sparse is cheap (a dict of token weights per text)
        # so we keep it on by default for Stage 3 hybrid retrieval.
        self._sparse_enabled: bool = True
        # Stage 3+ full-text search index (tantivy + jieba). Replaces
        # the bge-m3 sparse channel for hybrid retrieval because
        # bge-m3 sparse scored 0.0 on pure-number / short-CJK queries.
        # Created lazily on first use to avoid native-module import
        # cost when search isn't needed.
        self._fts_index: Any = None
        self._fts_index_dir: Path | None = None
        # When True, the next FTS read will rebuild the index from
        # LanceDB (used for one-time migration of v3 KBs).
        self._fts_needs_reindex: bool = True

    async def initialize(self) -> None:
        """Initialize LanceDB connection."""
        lancedb = _import_lancedb()
        self._db = lancedb.connect(str(self.data_dir))
        logger.info(f"LanceDB connected at {self.data_dir}")

    def _get_fts_index(self):
        """Lazily create / open the FTS index. First call also migrates
        existing v3 KBs (LanceDB chunks with no FTS index yet) by
        rebuilding FTS from the text field.
        """
        if self._fts_index is not None:
            return self._fts_index
        # Lazy import — tantivy pulls in a Rust extension
        from yuanwork.retrieval.fts import FTSIndex

        fts_dir = self.data_dir / "fts"
        self._fts_index_dir = fts_dir
        self._fts_index = FTSIndex(fts_dir)

        # One-time migration: if FTS is empty but LanceDB has rows,
        # rebuild FTS from LanceDB text. This makes the upgrade from
        # v3 (bge-m3 sparse only) seamless.
        if self._fts_needs_reindex:
            try:
                fts_count = self._fts_index.count()
                # Use the sync table.count_rows() here — the public
                # `self.count()` is async and we're inside a sync helper.
                lancedb_count = (
                    self._table.count_rows() if self._table is not None else 0
                )
            except Exception:
                fts_count, lancedb_count = 0, 0
            if fts_count == 0 and lancedb_count > 0:
                logger.info(
                    f"Migrating {lancedb_count} chunks from LanceDB to FTS index"
                )
                self._reindex_from_lancedb()
            self._fts_needs_reindex = False
        return self._fts_index

    def _reindex_from_lancedb(self) -> int:
        """Rebuild the FTS index from current LanceDB contents.

        Used by the migration path; called inside `_get_fts_index()`.
        """
        if self._fts_index is None or self._table is None:
            return 0
        try:
            import pandas as pd

            df = self._table.to_pandas()
            chunks = [
                {"id": r["id"], "doc_id": r["doc_id"], "text": r.get("text", "")}
                for _, r in df.iterrows()
            ]
            return self._fts_index.reindex_from_chunks(chunks)
        except Exception as e:
            logger.warning(f"FTS reindex from LanceDB failed: {e!r}")
            return 0

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

    def _expected_schema_fields(self) -> list[tuple[str, str]]:
        """Return [(name, pyarrow_type_name), ...] for the v2 chunks schema."""
        return [
            ("id", "string"),
            ("doc_id", "string"),
            ("chunk_idx", "int32"),
            ("text", "string"),
            ("vector", "list<float32>[1024]"),
            ("file_name", "string"),
            ("file_path", "string"),
            ("file_type", "string"),
            ("page", "int32"),
            ("slide_number", "int32"),
            ("section", "string"),
            ("content_type", "string"),
            ("token_count", "int32"),
            ("modified_at", "string"),
            ("created_at", "string"),
            # v2 (Stage 3): JSON-serialized bge-m3 lexical weights.
            # Format: {"<token>": <weight>, ...}. Empty string for non-text
            # rows (placeholders) or back-compat with v1 tables.
            ("sparse_str", "string"),
        ]

    def get_or_create_chunks_table(self):
        """Get or create the chunks table, with lazy schema migration.

        Migration policy:
          - New tables: created at current schema version.
          - Existing tables: detect missing v2 columns (sparse_str) and
            add them via `add_columns`. We don't try to back-fill old rows
            — sparse vectors are recomputed on next re-ingest of that doc.
        """
        if self._db is None:
            raise RuntimeError("SemanticMemory not initialized")

        if self._table is not None:
            return self._table

        # Cheap path: already exists
        try:
            if self.CHUNKS_TABLE in self._db.list_tables():
                self._table = self._db.open_table(self.CHUNKS_TABLE)
                self._migrate_chunks_table_if_needed()
                return self._table
        except Exception as e:
            logger.debug(f"list_tables() check failed: {e}")

        # Try opening directly (handles stale list_tables cache)
        try:
            self._table = self._db.open_table(self.CHUNKS_TABLE)
            self._migrate_chunks_table_if_needed()
            return self._table
        except Exception:
            pass

        # Create from scratch at current schema version
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
                ("sparse_str", pa.string()),
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
                self._migrate_chunks_table_if_needed()
            else:
                raise
        return self._table

    def _migrate_chunks_table_if_needed(self) -> None:
        """Add missing v2 columns to an existing chunks table.

        Currently a no-op for fresh v2 tables; for v1 tables, this adds
        the `sparse_str` column with empty-string default.
        """
        if self._table is None:
            return
        try:
            existing_fields = {f.name for f in self._table.schema}
        except Exception as e:
            logger.debug(f"could not read table schema: {e}")
            return
        expected = self._expected_schema_fields()
        missing = [(n, t) for n, t in expected if n not in existing_fields]
        if not missing:
            return
        logger.info(
            f"Migrating chunks table: adding columns {[n for n, _ in missing]}"
        )
        try:
            # add_columns on lancedb 0.6+ takes a pyarrow schema; for
            # single-column adds, also accept a list of (name, type) tuples
            import pyarrow as pa

            new_fields = []
            for name, type_name in missing:
                if type_name == "string":
                    new_fields.append(pa.field(name, pa.string()))
                elif type_name.startswith("list<float32>"):
                    new_fields.append(pa.field(name, pa.list_(pa.float32(), 1024)))
                elif type_name == "int32":
                    new_fields.append(pa.field(name, pa.int32()))
                else:
                    new_fields.append(pa.field(name, pa.string()))
            self._table.add_columns(new_fields)
            logger.info(f"Added {len(new_fields)} column(s) to chunks table")
        except Exception as e:
            logger.warning(f"Failed to migrate chunks table (will continue): {e!r}")

    @staticmethod
    def _sparse_to_json(sparse_vec: dict[str, float] | None) -> str:
        """Serialize a bge-m3 sparse dict to JSON. Returns "" for None/empty."""
        if not sparse_vec:
            return ""
        # Keep only non-trivial weights to slim down stored data
        return json.dumps(
            {str(k): float(v) for k, v in sparse_vec.items() if v > 0.0},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    @staticmethod
    def _sparse_from_json(s: str | None) -> dict[str, float]:
        """Reverse of _sparse_to_json. Empty dict for None / invalid JSON."""
        if not s:
            return {}
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                return {str(k): float(v) for k, v in data.items() if v}
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
        return {}

    @staticmethod
    def _sparse_dot(
        q: dict[str, float], d: dict[str, float]
    ) -> float:
        """Sparse lexical score = sum of q[w] * d[w] for shared tokens.

        This is the standard "Multi-Vector" lexical dot product, what
        bge-m3's `return_sparse` output is designed for. It naturally
        rewards exact-token overlap ("1280", "INV-2024-001", "金额")
        and gracefully handles CJK because bge-m3 tokenizes CJK into
        meaningful subword units.
        """
        if not q or not d:
            return 0.0
        if len(q) > len(d):
            q, d = d, q
        return sum(w * d[t] for t, w in q.items() if t in d)

    async def embed_texts(
        self,
        texts: list[str],
        batch_size: int = 8,
        return_sparse: bool | None = None,
    ) -> tuple[list[list[float]], list[dict[str, float]] | None]:
        """Embed texts using bge-m3. Returns (dense, sparse_or_None).

        `return_sparse` defaults to `self._sparse_enabled` so callers
        don't need to opt in. Pass `False` to skip the sparse channel
        (e.g. for back-compat callers or when only dense is needed).
        """
        if self._embedder is None:
            await self.load_embedding_model_async()

        if return_sparse is None:
            return_sparse = self._sparse_enabled

        all_dense: list[list[float]] = []
        all_sparse: list[dict[str, float]] | None = [] if return_sparse else None

        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            result = await asyncio.to_thread(
                self._embedder.encode,
                batch,
                return_dense=True,
                return_sparse=return_sparse,
                return_colbert_vecs=False,
            )
            dense = result["dense_vecs"]
            if hasattr(dense, "tolist"):
                dense = dense.tolist()
            all_dense.extend(dense)
            if return_sparse and all_sparse is not None:
                # bge-m3 key is "lexical_weights" — a list of
                # dict[str, float], one per input text.
                lw = result.get("lexical_weights") or []
                all_sparse.extend(lw)

        return all_dense, all_sparse

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

        # Embed all texts (dense + sparse)
        texts = [c["text"] for c in chunks]
        embeddings, sparse_vecs = await self.embed_texts(texts, return_sparse=True)

        from datetime import datetime

        now = datetime.now().isoformat()

        records = []
        for chunk, embedding, sparse in zip(chunks, embeddings, sparse_vecs or [None] * len(chunks)):
            # Normalize vector to fixed length (pad/truncate to 1024)
            vec = list(embedding)[:1024]
            while len(vec) < 1024:
                vec.append(0.0)

            # The chunker keeps the parser's metadata nested under "meta",
            # so the location has to be read from there - reading the flat
            # "page" key would store a null for every PDF and PPTX.
            meta = chunk_meta(chunk)

            record = {
                "id": chunk["id"],
                "doc_id": chunk["doc_id"],
                "chunk_idx": chunk.get("chunk_idx", 0),
                "text": chunk["text"],
                "vector": vec,
                "file_name": chunk.get("file_name", ""),
                "file_path": chunk.get("file_path", ""),
                "file_type": chunk.get("file_type", "unknown"),
                "page": meta.get("page"),
                "slide_number": meta.get("slide_number"),
                # The DOCX / Markdown parsers call the heading "heading";
                # the column is named "section".
                "section": meta.get("heading") or meta.get("section"),
                "content_type": chunk.get("content_type", "text"),
                "token_count": chunk.get("token_count", len(chunk["text"]) // 4),
                "modified_at": chunk.get("modified_at", now),
                "created_at": now,
                "sparse_str": self._sparse_to_json(sparse),
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

        # Mirror into the FTS index. Best-effort: if tantivy/jieba are
        # missing or fail, dense-only retrieval still works (we just lose
        # the literal-token rescue channel). We log and continue.
        try:
            fts = self._get_fts_index()
            # The migration check inside _get_fts_index() runs once and
            # only when fts_count==0 AND lancedb_count>0. We've just
            # added N rows to lancedb, so on a *truly* fresh KB the
            # migration would otherwise pull these same N rows from
            # lancedb and then add_documents would double them. Mark
            # migration as "not needed anymore" so it doesn't run
            # mid-add_chunks.
            self._fts_needs_reindex = False
            # Delete-then-add: if the same IDs were indexed before
            # (re-ingestion), remove them first so add_documents doesn't
            # double up. This mirrors the table.delete(f"id IN (...)")
            # pattern used for LanceDB above.
            existing_ids = [c["id"] for c in chunks]
            await asyncio.to_thread(fts.delete_by_ids, existing_ids)
            fts_docs = [
                {"id": c["id"], "doc_id": c["doc_id"], "text": c["text"]}
                for c in chunks
            ]
            # FTS add is sync (tantivy is fast). Run in a thread so we
            # don't block the event loop for big batches.
            await asyncio.to_thread(fts.add_documents, fts_docs)
        except Exception as e:
            logger.warning(f"FTS index add failed (dense-only fallback): {e!r}")

        logger.info(f"Added {len(records)} chunks to LanceDB + FTS")
        return [c["id"] for c in chunks]

    async def search(
        self,
        query: str,
        top_k: int = 10,
        filter_expr: str | None = None,
    ) -> list[dict[str, Any]]:
        """Pure dense vector similarity search (back-compat).

        For Stage 3 hybrid retrieval, prefer `search_hybrid` which fuses
        dense + sparse and rescues literal-token queries.
        """
        table = self.get_or_create_chunks_table()

        # Short-circuit when the KB is empty. This avoids triggering the
        # heavy bge-m3 model load on a fresh install.
        if table.count_rows() == 0:
            return []

        # Embed query (dense only for this method)
        dense_q, _ = await self.embed_texts([query], return_sparse=False)
        query_vec = dense_q[0][:1024]
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

    async def search_hybrid(
        self,
        query: str,
        top_k: int = 10,
        filter_expr: str | None = None,
        alpha: float = 0.65,
        beta: float = 0.35,
        recall_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid dense + sparse retrieval (Stage 3 default).

        Args:
            query: Search query text
            top_k: Number of final results
            filter_expr: Optional LanceDB filter expression
            alpha: weight for dense score
            beta: weight for sparse score
            recall_k: how many candidates to fetch per channel before
                fusion (default = max(top_k * 3, 30))

        Returns:
            List of chunk dicts, each with `_score` (combined) and
            `_score_dense` / `_score_sparse` sub-scores for debugging.
        """
        if recall_k is None:
            recall_k = max(top_k * 3, 30)

        table = self.get_or_create_chunks_table()
        if table.count_rows() == 0:
            return []

        # Embed query with both channels. Note: the bge-m3 sparse
        # channel is still requested (so the column stays populated for
        # back-compat) but no longer drives retrieval — see Channel 2
        # below for the Tantivy FTS path.
        dense_q, sparse_q_list = await self.embed_texts(
            [query], return_sparse=True
        )
        dense_q = dense_q[0]

        # Pad dense to 1024 (bge-m3 already returns 1024 but be safe)
        dense_q = list(dense_q)[:1024]
        while len(dense_q) < 1024:
            dense_q.append(0.0)

        # --- Channel 1: dense vector recall ---
        if filter_expr:
            dense_results = (
                table.search(dense_q).where(filter_expr).limit(recall_k).to_list()
            )
        else:
            dense_results = table.search(dense_q).limit(recall_k).to_list()
        for r in dense_results:
            distance = r.pop("_distance", 0.0)
            r["_score_dense"] = 1.0 / (1.0 + distance)

        # --- Channel 2: sparse lexical recall (tantivy FTS + jieba) ---
        # The FTS index is the new sparse channel. It gives proper BM25
        # scoring on jieba-segmented text, so "1280", "金额", "INV-2024-001"
        # all match via real token overlap. bge-m3's lexical_weights
        # underweighted these, which is why we replaced it.
        #
        # FTS results carry only (id, score). We need the full chunk row
        # for the response (text, file_name, etc.), so we look up each
        # hit in the dense result set first; if the FTS hit is not in
        # the dense top-K we fetch it directly from the table.
        dense_ids = {r["id"]: r for r in dense_results}
        sparse_results: list[dict[str, Any]] = []
        try:
            fts = self._get_fts_index()
            fts_hits = await asyncio.to_thread(fts.search, query, recall_k)
        except Exception as e:
            logger.warning(f"FTS search failed (sparse channel empty): {e!r}")
            fts_hits = []
        for cid, fts_score in fts_hits:
            base = dense_ids.get(cid)
            if base is None:
                # FTS hit not in dense top-K — pull it from the table.
                try:
                    if filter_expr:
                        rows = (
                            table.search(dense_q)
                            .where(f"{filter_expr} AND id = '{cid}'")
                            .limit(1)
                            .to_list()
                        )
                    else:
                        rows = (
                            table.search(dense_q)
                            .where(f"id = '{cid}'")
                            .limit(1)
                            .to_list()
                        )
                except Exception:
                    rows = []
                if not rows:
                    continue
                base = rows[0]
                # base is from a dummy vector search so its _distance is
                # meaningless; drop it
                base.pop("_distance", None)
                base["_score_dense"] = 0.0
            r2 = dict(base)
            r2["_score_sparse"] = float(fts_score)
            sparse_results.append(r2)
        # Sort by sparse score, take top recall_k
        sparse_results.sort(key=lambda r: r.get("_score_sparse", 0.0), reverse=True)
        sparse_results = sparse_results[:recall_k]

        # --- Fusion: weighted sum of normalized scores ---
        # We normalize each channel to its own max so the two weight
        # parameters are interpretable as "fraction of total signal".
        def _normalize(rs: list[dict[str, Any]], key: str) -> dict[str, float]:
            if not rs:
                return {}
            max_s = max((r.get(key, 0.0) for r in rs), default=0.0)
            if max_s <= 0:
                return {r["id"]: 0.0 for r in rs}
            return {r["id"]: r.get(key, 0.0) / max_s for r in rs}

        dense_norm = _normalize(dense_results, "_score_dense")
        sparse_norm = _normalize(sparse_results, "_score_sparse")

        all_ids = set(dense_norm) | set(sparse_norm)
        fused: list[dict[str, Any]] = []
        for cid in all_ids:
            d = dense_norm.get(cid, 0.0)
            s = sparse_norm.get(cid, 0.0)
            combined = alpha * d + beta * s
            # Pick the row with the most metadata (dense channel usually
            # carries the full row; sparse can fall back to it).
            base = next(
                (r for r in dense_results if r["id"] == cid),
                next(
                    (r for r in sparse_results if r["id"] == cid),
                    {"id": cid},
                ),
            )
            row = dict(base)
            row.pop("_distance", None)
            row["_score"] = combined
            row["_score_dense"] = d
            row["_score_sparse"] = s
            fused.append(row)
        fused.sort(key=lambda r: r["_score"], reverse=True)
        return fused[:top_k]

    async def delete_by_doc_id(self, doc_id: str) -> int:
        """Delete all chunks for a document (LanceDB + FTS)."""
        table = self.get_or_create_chunks_table()
        try:
            table.delete(f"doc_id = '{doc_id}'")
        except Exception as e:
            logger.warning(f"Failed to delete chunks for doc {doc_id}: {e}")
            return 0
        # Mirror deletion into FTS (best-effort)
        try:
            fts = self._get_fts_index()
            await asyncio.to_thread(fts.delete_by_doc_id, doc_id)
        except Exception as e:
            logger.warning(f"FTS delete for doc {doc_id} failed: {e!r}")
        return 1  # LanceDB doesn't return count

    async def count(self) -> int:
        """Count total chunks."""
        try:
            table = self.get_or_create_chunks_table()
            return table.count_rows()
        except Exception:
            return 0
