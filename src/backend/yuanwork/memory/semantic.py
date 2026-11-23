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
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np

from yuanwork.memory.encoder import SlicedEncoder
from yuanwork.utils.chunk_meta import chunk_meta
from yuanwork.utils.model_load import load_model_offline_first

logger = logging.getLogger(__name__)


# Lazy imports for heavy deps
def _import_lancedb():
    import lancedb

    return lancedb


def _import_flag_embedding():
    from FlagEmbedding import BGEM3FlagModel

    return BGEM3FlagModel


#: How many rows the vector store takes in one write, when the setting
#: is not around.
DEFAULT_WRITE_BATCH_SIZE = 256

#: How many documents the full-text index keeps in the writer's hands
#: before it flushes on its own is the index's own business
#: (`DEFAULT_COMMIT_THRESHOLD` in the retrieval layer, ADR-043): the
#: flush is a disk write, and a build that flushes per batch pays for
#: one per batch.

#: The batch size the embedding timeout was written for. The timeout is
#: a budget for a full batch of this many slices; a bigger batch gets
#: proportionally more time, a smaller one less.
EMBEDDING_TIMEOUT_REFERENCE_BATCH = 8

#: How many times longer the longest slice of a batch may be than its
#: shortest one. A batch pads to its longest slice, so a batch that
#: mixes a one-line note with a whole page spends most of its work on
#: padding. Measured on real slices: sorting by length and cutting
#: into fixed-size batches of 32 makes the padding *worse* (74% against
#: 49% for the unsorted batches of 8) - a batch of 32 reaches so much
#: further into the long tail that every slice in it pays for it.
#: Splitting the sorted slices into windows instead brings the padding
#: down to 17%.
EMBEDDING_LENGTH_RATIO = 2.0


def _batches_by_length(
    texts: list[str], batch_size: int, ratio: float = EMBEDDING_LENGTH_RATIO
) -> list[list[int]]:
    """The batches the texts go into, and where each text ends up.

    The texts are sorted by length and then cut into windows: a window
    ends when it is full, or when the next text is more than `ratio`
    times longer than the window's shortest one. The padding a batch
    costs is decided by its longest slice, so the batches are made of
    slices of similar length - and a batch of 32 can then be as cheap
    as 32 batches of 8.

    The result is the positions of the texts in the original list, so
    the caller can put the answers back where they belong.
    """
    order = sorted(range(len(texts)), key=lambda i: len(texts[i]))

    batches: list[list[int]] = []
    current: list[int] = []
    shortest = 0

    for index in order:
        length = len(texts[index])
        if current and (
            len(current) >= batch_size
            or (shortest and length > shortest * ratio)
        ):
            batches.append(current)
            current = []
            shortest = 0
        if not current:
            shortest = length
        current.append(index)

    if current:
        batches.append(current)
    return batches


def _as_float_array(embedding: Any) -> np.ndarray:
    """A vector as a flat float32 array.

    The model hands back numpy arrays; the store wants float32. Going
    through a Python list would build a million small objects on the
    way - Arrow takes the array as it is.
    """
    if isinstance(embedding, np.ndarray):
        return embedding.reshape(-1).astype(np.float32, copy=False)
    return np.asarray(embedding, dtype=np.float32).reshape(-1)


def _table_has_rows(table: Any) -> bool:
    """Whether the table already holds rows.

    A table that cannot say (a stand-in, a store that changed) is
    treated as if it has rows: the delete that follows is harmless on
    an empty table, while skipping it on a full one would leave the
    old copy of a re-ingested document in the index.
    """
    counter = getattr(table, "count_rows", None)
    if counter is None:
        return True
    try:
        return int(counter()) > 0
    except Exception:  # noqa: BLE001 - an unreadable table is a full table
        return True


def _quantize_int8(model: Any) -> Any:
    """The model with its linear layers in int8.

    Dynamic quantization: the weights become int8 and the activations
    are quantized on the fly. On the CPU it is about twice as fast,
    and the vectors move a little - so it is a setting and not a
    default, and a model that cannot be quantized is left as it is
    rather than failing the build.
    """
    import warnings

    import torch

    inner = getattr(model, "model", None)
    if inner is None:
        logger.warning("The model has no inner transformer - no int8 for it")
        return model

    try:
        from torch.ao.quantization import quantize_dynamic

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            quantized = quantize_dynamic(
                inner, {torch.nn.Linear}, dtype=torch.qint8, inplace=False
            )
    except Exception as e:  # noqa: BLE001 - a failed quantization is not a failed build
        logger.warning(f"int8 quantization did not work, keeping the model as it is: {e}")
        return model

    model.model = quantized
    return model

# Schema versioning: when the chunks table schema evolves, bump this and
# add a migration block in `get_or_create_chunks_table`. v1 = initial.
# v2 = added `sparse_str` column for bge-m3 lexical weights.
CHUNKS_SCHEMA_VERSION = 2


def spread_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Evenly spread over the rows - the first, the last, and the ones
    in between.

    A summary of a document needs the document's shape, not its first
    few paragraphs: taking every n-th row shows what the document
    covers from beginning to end (TASK-085).
    """
    if limit <= 0 or not rows:
        return []
    if len(rows) <= limit:
        return list(rows)
    step = (len(rows) - 1) / (limit - 1) if limit > 1 else 1
    return [rows[int(round(i * step))] for i in range(limit)]


class SemanticMemory:
    """LanceDB-backed semantic memory store."""

    CHUNKS_TABLE = "chunks"

    #: Class-level defaults. An instance built without __init__ (the
    #: tests do that to avoid loading the model) still has them - and
    #: the safe value is the one that asks for less work.
    _sparse_enabled: bool = False
    write_batch_size: int = DEFAULT_WRITE_BATCH_SIZE
    _embedder: Any = None
    _encoder: "SlicedEncoder | None" = None

    def __init__(
        self,
        data_dir: Path | str,
        embedding_model: str = "BAAI/bge-m3",
        embedding_device: str = "auto",
        dense_timeout: float | None = None,
        sparse_timeout: float | None = None,
        embedding_timeout: float | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.embedding_model_name = embedding_model
        # Which device the embedding runs on, and why it is that one.
        # "auto" means: the GPU when there is one, the CPU otherwise.
        self.embedding_device, self.device_fallback_reason = self._resolve_device(
            embedding_device
        )
        from yuanwork.config import get_settings

        settings = get_settings()
        self.embedding_max_tokens = settings.embedding_max_tokens
        self.embedding_use_fp16 = settings.embedding_use_fp16
        #: "none" or "int8" - how the model's arithmetic is done.
        self.embedding_quantize = str(
            getattr(settings, "embedding_quantize", "none") or "none"
        ).strip().lower()
        # The embedding batch size is configuration: it is the biggest
        # lever on how long the first build takes. Zero means "the
        # device decides" - the batch is then cut by what the machine
        # can hold, measured at runtime (ADR-044). A number here is a
        # cap the device may not go past.
        self.embedding_batch_size = settings.embedding_batch_size
        # How many rows the vector store takes in one write. A build
        # hands over a whole batch; the store writes it in pieces so
        # the peak memory and the write latency stay bounded (AC-P-007).
        self.write_batch_size = settings.index_write_batch_size
        if dense_timeout is None:
            dense_timeout = settings.dense_timeout_seconds
        if sparse_timeout is None:
            sparse_timeout = settings.sparse_timeout_seconds
        if embedding_timeout is None:
            embedding_timeout = settings.embedding_timeout_seconds
        # How long each step of the retrieval chain may take. The dense
        # channel is the one that cannot be dropped, so its timeout
        # fails the query; the full-text channel degrades to dense-only
        # (design 4.3). What neither may do is hang.
        self.dense_timeout = dense_timeout
        self.sparse_timeout = sparse_timeout
        self.embedding_timeout = embedding_timeout
        self._db = None
        self._table = None
        self._embedder = None
        #: The encoder that cuts the batches by what the device can take.
        #: Made when the model is loaded.
        self._encoder: SlicedEncoder | None = None
        self._embedding_dim: int | None = None
        # Set to True while a background prewarm is in flight so callers
        # (e.g. /health) can report "loading" instead of pretending the
        # model is ready.
        self._prewarm_in_flight: bool = False
        self._prewarm_task: asyncio.Task | None = None
        #: The model is loaded at most once at a time. The prewarm in
        #: the background and a question that arrives while it is still
        #: running must not each load their own copy (see
        #: `load_embedding_model`).
        self._load_lock = threading.Lock()
        # Toggle: when True, embed_texts also asks bge-m3 for sparse
        # vectors. Off by default: the retrieval side does not use
        # them (the sparse channel is the full-text index), and asking
        # bge-m3 for lexical weights costs real time on every slice -
        # on the CPU it is a big part of the embedding bill.
        self._sparse_enabled: bool = bool(
            getattr(settings, "embedding_sparse_channel", False)
        )
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
        #: How long the last write took, in seconds: the embedding and
        #: the writing. The benchmark reads them to say where the time
        #: of a build went (AC-P-011 / AC-P-012).
        self.last_timings: dict[str, float] = {"embed": 0.0, "write": 0.0}

    @staticmethod
    def _resolve_device(device: str) -> tuple[str, str]:
        """Which device the embedding runs on, and why.

        The GPU is worth a lot of time on a build (ADR-035) - but only
        when there is one. When there is not, the reason is written
        down: a build that is slow for no visible reason is the kind
        of thing a person loses an afternoon to.

        The reason says *which* thing is missing. The card may not be
        there - or the card may be sitting in the machine with a torch
        build that has no CUDA in it. Those two need different fixes,
        and "no graphics card" when the card is right there sends the
        user looking for the wrong thing.
        """
        wanted = (device or "auto").strip().lower()
        if wanted not in ("auto", "cpu", "cuda"):
            return "cpu", f"配置的设备「{device}」不认识，按 CPU 处理"

        try:
            import torch
        except Exception as e:  # noqa: BLE001 - torch may not be there at all
            return "cpu", f"torch 不可用（{e!r}），按 CPU 处理"

        cuda = bool(torch.cuda.is_available())
        if cuda:
            no_cuda = ""
        elif getattr(torch.version, "cuda", None):
            no_cuda = "torch 找不到可用的 NVIDIA 显卡（驱动没认到这台设备）"
        else:
            no_cuda = "torch 是 CPU 版构建（没有 CUDA 支持），显卡用不上"

        if wanted == "cpu":
            return "cpu", ""
        if wanted == "cuda":
            if cuda:
                return "cuda", ""
            return "cpu", f"配置要求用 CUDA，但{no_cuda}"

        if cuda:
            return "cuda", ""
        return "cpu", f"{no_cuda}，按 CPU 处理"

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

    async def flush_fts(self) -> None:
        """Make everything written to the full-text index searchable.

        The write path leaves the documents in the index's own hands
        (ADR-043): it decides when to flush them to the disk. This is
        the flush that the build ends with - after it, the long tail is
        searchable (AC-F-026).
        """
        try:
            fts = self._get_fts_index()
            await asyncio.to_thread(fts.commit)
        except Exception as e:  # noqa: BLE001 - a failed flush is not a failed build
            logger.warning(f"The full-text index was not flushed: {e!r}")

    async def close(self) -> None:
        """Close the database (LanceDB doesn't need explicit close but be safe)."""
        # The full-text index may still be holding documents that were
        # not flushed - a build writes in batches, and the last one has
        # to reach the disk before the index is closed (TASK-058).
        try:
            if getattr(self, "_fts_index", None) is not None:
                await asyncio.to_thread(self._fts_index.commit)
        except Exception as e:  # noqa: BLE001 - a failed flush is not a failed close
            logger.warning(f"The full-text index was not flushed: {e!r}")
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

        with self._load_lock:
            if self._embedder is not None:
                # The load we were queued behind finished while we waited -
                # its model is the model.
                return
            self._load_embedding_model()

    def _load_embedding_model(self) -> None:
        """The load itself. It runs under the lock.

        Only one load at a time: the backend prewarms the model in the
        background at startup, and a question that arrives while that
        load is still running would otherwise start a *second* one - two
        copies of a 2.3 GB model on the same card, and an encoder left
        pointing at one copy while the model field points at the other.
        The query vector then comes out in the wrong precision and the
        vector channel dies ("mat1 and mat2 must have the same dtype"),
        which the retrieval swallows - the app then answers "the
        documents do not say that" about documents that do say it
        (AC-F-004).
        """
        BGEM3FlagModel = _import_flag_embedding()

        # Suppress noisy output from FlagEmbedding
        os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

        logger.info(f"Loading embedding model {self.embedding_model_name} on {self.embedding_device}...")
        # Half precision is worth it on the GPU and nothing on the CPU.
        # The token budget belongs to the model: one piece of text is
        # at most this many tokens (ADR-035).
        self._embedder = load_model_offline_first(
            BGEM3FlagModel,
            self.embedding_model_name,
            devices=self.embedding_device,
            use_fp16=bool(
                self.embedding_use_fp16 and self.embedding_device == "cuda"
            ),
            max_length=self.embedding_max_tokens,
        )
        if self.embedding_quantize == "int8":
            self._embedder = _quantize_int8(self._embedder)
        # The encoder cuts the batches by what this device can take. It
        # is made here, when the model and the device are both known.
        self._encoder = SlicedEncoder(
            self._embedder,
            self.embedding_device,
            self.embedding_max_tokens,
        )
        if self.embedding_batch_size:
            # A number in the settings is a cap: the device may go
            # smaller on its own, never bigger.
            self._encoder.set_sequence_cap(self.embedding_batch_size)
        logger.info(
            "Embedding model loaded"
            + (" (int8)" if self.embedding_quantize == "int8" else "")
            + f" on {self._encoder.profile.note}"
        )

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
            # The position of the chunk in its document: one structure with
            # a kind and the coordinates that kind defines. It replaces the
            # three fixed columns (page / slide_number / section), which
            # had nowhere to put an Excel row or a heading path.
            ("anchor", "string"),
            ("content_type", "string"),
            ("token_count", "int32"),
            ("modified_at", "string"),
            ("created_at", "string"),
            # v2 (Stage 3): JSON-serialized bge-m3 lexical weights.
            # Format: {"<token>": <weight>, ...}. Empty string for non-text
            # rows (placeholders) or back-compat with v1 tables.
            ("sparse_str", "string"),
        ]

    def _expected_schema_fields_names(self) -> list[str]:
        """The names of the chunk columns, in schema order."""
        return [name for name, _ in self._expected_schema_fields()]

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
                ("anchor", pa.string()),
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

    def _batch_timeout(self, count: int) -> float:
        """How long a batch of this many slices may take.

        The setting is a budget for a full batch of eight slices; a
        bigger batch gets proportionally more time and a smaller one
        less. A fixed number would either give up on a slow machine
        or wait forever on a fast one.
        """
        return (
            self.embedding_timeout
            * max(1, int(count))
            / EMBEDDING_TIMEOUT_REFERENCE_BATCH
        )

    async def _embed_one_batch(
        self,
        batch: list[str],
        return_sparse: bool,
        timeout: float,
    ) -> tuple[list[Any], list[dict[str, float]]]:
        """One batch through the model.

        The timeout is a budget for a full batch of eight slices, so a
        bigger batch gets proportionally more time and a smaller one
        less. A fixed 60 seconds is plenty for eight slices on a GPU
        and far too little for thirty-two on a CPU, where the same
        work takes ten times as long - and a build that gives up
        halfway is worse than a slow one.
        """
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self._embedder.encode,
                    batch,
                    return_dense=True,
                    return_sparse=return_sparse,
                    return_colbert_vecs=False,
                ),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"嵌入模型推理超过 {timeout:.0f} 秒"
            ) from None

        dense = result["dense_vecs"]
        if hasattr(dense, "tolist"):
            dense = dense.tolist()
        sparse: list[dict[str, float]] = []
        if return_sparse:
            # bge-m3 key is "lexical_weights" — a list of
            # dict[str, float], one per input text.
            sparse = list(result.get("lexical_weights") or [])
        return dense, sparse

    async def embed_arrays(
        self, texts: list[str], batch_size: int | None = None
    ) -> np.ndarray:
        """Embed texts and hand back the vectors as arrays.

        This is the path the writer uses: the vectors go straight into
        Arrow, and turning 1024 floats per slice into Python objects and
        back again on the way is pure waste.

        The batch is cut by the encoder - by what the device can take,
        measured while it runs - so the caller does not have to know
        whether it is talking to a card or to a processor. A
        ``batch_size`` given here caps it.
        """
        if self._embedder is None:
            await self.load_embedding_model_async()

        if not texts:
            return np.zeros((0, 0), dtype=np.float32)

        encoder = self._encoder
        if encoder is None or not encoder.can_encode_here():
            # The library's own path - for an embedder whose internals
            # do not look the way the encoder expects.
            dense, _ = await self._embed_through_library(
                texts, batch_size=batch_size
            )
            return np.asarray(dense, dtype=np.float32)

        return await asyncio.wait_for(
            asyncio.to_thread(encoder.encode, texts, batch_size),
            timeout=self._batch_timeout(len(texts)),
        )

    async def embed_texts(
        self,
        texts: list[str],
        batch_size: int | None = None,
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

        if not return_sparse and not texts:
            return [], None

        # The sparse channel is the library's own - the fast path does
        # not compute lexical weights, and the retrieval side does not
        # read them anyway (the full-text index is the FTS one).
        if return_sparse:
            return await self._embed_through_library(
                texts, return_sparse=True, batch_size=batch_size
            )

        vectors = await self.embed_arrays(texts, batch_size=batch_size)
        return [vector.tolist() for vector in vectors], None

    async def _embed_through_library(
        self,
        texts: list[str],
        return_sparse: bool = False,
        batch_size: int | None = None,
    ) -> tuple[list[list[float]], list[dict[str, float]] | None]:
        """The embedding library's own path.

        The texts are batched by length, not by position. A batch
        pads to its longest text, so a batch of mixed lengths spends
        a good part of its work on padding - and the slices of a
        document run from a sentence to a page. The batches are cut
        into windows of similar length, so a big batch does not reach
        into the long tail and make everybody pay for it. The order
        is put back afterwards; the caller does not care how it was
        batched.
        """
        if batch_size is None:
            batch_size = self.embedding_batch_size
        batch_size = max(1, int(batch_size or 32))
        batches = _batches_by_length(texts, batch_size)

        dense_ordered: list[tuple[int, Any]] = []
        sparse_ordered: list[tuple[int, dict[str, float]]] = []

        for positions in batches:
            batch = [texts[position] for position in positions]
            dense, sparse = await self._embed_one_batch(
                batch, return_sparse, self._batch_timeout(len(batch))
            )
            # The answers go back where the texts came from: the caller
            # does not care how they were batched, but it does care that
            # the answers match the questions.
            for offset, position in enumerate(positions):
                dense_ordered.append((position, dense[offset]))
                if offset < len(sparse):
                    sparse_ordered.append((position, sparse[offset]))

        all_dense: list[Any] = [None] * len(texts)
        for position, vector in dense_ordered:
            all_dense[position] = vector

        all_sparse: list[dict[str, float]] | None = None
        if return_sparse:
            all_sparse = [None] * len(texts)  # type: ignore[list-item]
            for position, weights in sparse_ordered:
                all_sparse[position] = weights

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

        # Embed all texts. The bge-m3 sparse channel is only asked for
        # when it is switched on - the retrieval side does not read it
        # (the sparse channel is the full-text index), and computing it
        # costs real time on every slice.
        texts = [c["text"] for c in chunks]
        embed_started = time.perf_counter()
        if self._sparse_enabled:
            # The sparse channel is the library's own; the vectors come
            # back as Python lists and are turned into arrays here.
            embeddings, sparse_vecs = await self.embed_texts(
                texts, return_sparse=True
            )
            embeddings = [np.asarray(e, dtype=np.float32) for e in embeddings]
        else:
            # The vectors go straight into Arrow - no Python objects on
            # the way (a build of fifty thousand slices would otherwise
            # build fifty million of them).
            embeddings = await self.embed_arrays(texts)
            sparse_vecs = None
        embed_seconds = time.perf_counter() - embed_started

        from datetime import datetime

        now = datetime.now().isoformat()

        # A table created before the anchor existed still has the three
        # fixed location columns. They are filled from the anchor so the
        # old columns keep working - but the anchor is the position of
        # record.
        try:
            table_fields = {f.name for f in table.schema}
        except Exception:  # noqa: BLE001 - a schema we cannot read is a schema we trust
            table_fields = set(self._expected_schema_fields_names())

        records = []
        for chunk, embedding, sparse in zip(chunks, embeddings, sparse_vecs or [None] * len(chunks)):
            # Normalize vector to fixed length (pad/truncate to 1024).
            # The vector stays a numpy array: turning 1024 floats into a
            # Python list per chunk is 26 million Python objects per
            # thousand slices, and Arrow takes the array as it is.
            vec = _as_float_array(embedding)[:1024]
            if len(vec) < 1024:
                padded = np.zeros(1024, dtype=np.float32)
                padded[: len(vec)] = vec
                vec = padded

            # The chunker keeps the parser's metadata nested under "meta",
            # so the location has to be read from there - reading the flat
            # "page" key would store a null for every PDF and PPTX.
            meta = chunk_meta(chunk)
            anchor = meta.get("anchor")

            record = {
                "id": chunk["id"],
                "doc_id": chunk["doc_id"],
                "chunk_idx": chunk.get("chunk_idx", 0),
                "text": chunk["text"],
                "vector": vec,
                "file_name": chunk.get("file_name", ""),
                "file_path": chunk.get("file_path", ""),
                "file_type": chunk.get("file_type", "unknown"),
                "anchor": json.dumps(anchor, ensure_ascii=False) if anchor else "",
                "content_type": chunk.get("content_type", "text"),
                "token_count": chunk.get("token_count", len(chunk["text"]) // 4),
                "modified_at": chunk.get("modified_at", now),
                "created_at": now,
                "sparse_str": self._sparse_to_json(sparse),
            }

            if "page" in table_fields:
                record["page"] = meta.get("page")
                record["slide_number"] = meta.get("slide_number")
                # The DOCX / Markdown parsers call the heading "heading";
                # the column is named "section".
                record["section"] = meta.get("heading") or meta.get("section")

            records.append(record)

        write_started = time.perf_counter()

        # Delete existing chunks with same IDs (for re-ingestion).
        # On a knowledge base that is still empty there is nothing to
        # delete - and asking anyway means a scan of the table for
        # every batch, which on a big build is a lot of scanning for
        # nothing.
        existing_ids = [c["id"] for c in chunks]
        if _table_has_rows(table):
            try:
                table.delete(f"id IN ({','.join(repr(i) for i in existing_ids)})")
            except Exception:
                pass  # Table might be empty

        # Add new records, in pieces. One write of ten thousand rows
        # holds ten thousand vectors in memory at once; a build of
        # thousands of files hands over that much in one go (AC-P-007).
        size = max(1, int(getattr(self, "write_batch_size", DEFAULT_WRITE_BATCH_SIZE)))
        for start in range(0, len(records), size):
            table.add(records[start : start + size])

        # Mirror into the FTS index. Best-effort: if tantivy/jieba are
        # missing or fail, dense-only retrieval still works (we just lose
        # the literal-token rescue channel). We log and continue.
        #
        # The mirror does not hold up the build: the next batch's
        # embedding starts while this one is still being written into
        # the full-text index (TASK-064). The index itself decides when
        # to flush - it keeps the documents in the writer's hands until
        # there are enough of them (ADR-043) - and the build flushes it
        # once at the end, so the long tail is searchable when the build
        # says it is done (AC-F-026).
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
            fts_docs = [
                {"id": c["id"], "doc_id": c["doc_id"], "text": c["text"]}
                for c in chunks
            ]

            def _write_fts() -> None:
                # The delete and the add share one flush, and the flush
                # is the index's own decision (ADR-043): the documents
                # stay in the writer's hands until there are enough of
                # them, and the build flushes once at the end. The order
                # inside the writer is kept: the delete comes first, so
                # the new version of a chunk is the one that survives.
                fts.delete_by_ids(existing_ids, commit=False)
                fts.add_documents(fts_docs, commit=False)

            # The write is sync (tantivy is fast). It runs in a thread
            # so it does not block the event loop.
            #
            # It is **not** moved to a thread of its own: jieba's
            # segmentation is Python code, and on a machine whose cores
            # are already busy with the parsing workers it takes the
            # embedding's own time away (measured: the embedding went
            # from 371 to 460 seconds, and the build got slower even
            # though the write left the critical path). The flush is
            # what is worth deferring - that one is a disk write, not
            # CPU.
            await asyncio.to_thread(_write_fts)
        except Exception as e:
            logger.warning(f"FTS index add failed (dense-only fallback): {e!r}")

        self.last_timings = {
            "embed": embed_seconds,
            "write": time.perf_counter() - write_started,
        }

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
        def _dense_search() -> list[dict[str, Any]]:
            if filter_expr:
                return (
                    table.search(query_vec).where(filter_expr).limit(top_k).to_list()
                )
            return table.search(query_vec).limit(top_k).to_list()

        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(_dense_search), timeout=self.dense_timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"稠密检索超过 {self.dense_timeout} 秒"
            ) from None

        # Extract distance (LanceDB returns _distance; smaller = closer)
        for r in results:
            distance = r.pop("_distance", 0.0)
            # Convert to similarity (1 / (1 + distance))
            r["_score"] = 1.0 / (1.0 + distance)

        return results

    async def indexed_doc_ids(self) -> set[str]:
        """The documents the vector table actually holds.

        The ledger in SQLite says which files were indexed - but it can
        be wrong: a build that was interrupted after the ledger was
        written and before the slices were flushed leaves files that
        are "indexed" with nothing in the index (TASK-088). This is the
        truth, read from the index itself.

        Only the ids are read (a projection), so the whole table costs
        about a fifth of a second - cheap enough to ask once per build.
        """
        table = self.get_or_create_chunks_table()
        if table.count_rows() == 0:
            return set()

        def _read() -> set[str]:
            rows = (
                table.search()
                .select(["doc_id"])
                .limit(1_000_000)
                .to_list()
            )
            return {r.get("doc_id") for r in rows if r.get("doc_id")}

        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_read), timeout=self.dense_timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Reading the index's document ids took too long")
            return set()

    async def chunks_of_doc(
        self,
        doc_id: str,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """A spread of one document's chunks, in the order they appear.

        A summary of a document needs the document's *breadth* - what
        it covers from beginning to end - not the ten chunks that happen
        to match the question. When the question is "summarise this
        file", the file name is the query, and the similarity ranking
        says nothing about what the file is about (TASK-085).

        The chunks are read straight out of the table (no embedding, no
        reranking) and spread evenly over the document, so the answer
        sees the whole thing at a glance.
        """
        table = self.get_or_create_chunks_table()
        if table.count_rows() == 0:
            return []

        def _read() -> list[dict[str, Any]]:
            rows = (
                table.search()
                .where(f"doc_id = '{doc_id}'")
                .limit(2000)
                .to_list()
            )
            rows.sort(key=lambda r: r.get("chunk_idx") or 0)
            return rows

        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(_read), timeout=self.dense_timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Reading a document's chunks took too long")
            return []

        return spread_rows(rows, limit)

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

        # Embed the query. The bge-m3 sparse channel is only asked for
        # when it is switched on — it does not drive retrieval (see
        # Channel 2 below for the Tantivy FTS path), and computing it
        # costs time on every question.
        dense_q, sparse_q_list = await self.embed_texts(
            [query], return_sparse=self._sparse_enabled
        )
        dense_q = dense_q[0]

        # Pad dense to 1024 (bge-m3 already returns 1024 but be safe)
        dense_q = list(dense_q)[:1024]
        while len(dense_q) < 1024:
            dense_q.append(0.0)

        # --- Channel 1: dense vector recall ---
        # The vector store call is synchronous and gets slow as the table
        # grows, so it runs in a thread with a cap. The dense channel is
        # the one that cannot be dropped - a timeout fails the query
        # rather than quietly returning nothing (design 4.3).
        def _dense_search() -> list[dict[str, Any]]:
            if filter_expr:
                return (
                    table.search(dense_q).where(filter_expr).limit(recall_k).to_list()
                )
            return table.search(dense_q).limit(recall_k).to_list()

        try:
            dense_results = await asyncio.wait_for(
                asyncio.to_thread(_dense_search), timeout=self.dense_timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"稠密检索超过 {self.dense_timeout} 秒"
            ) from None
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
            fts_hits = await asyncio.wait_for(
                asyncio.to_thread(fts.search, query, recall_k),
                timeout=self.sparse_timeout,
            )
        except Exception as e:
            # The full-text channel is the one that can be dropped: the
            # query still answers from the dense channel alone.
            logger.warning(f"FTS search failed (sparse channel empty): {e!r}")
            fts_hits = []
        # The hits the dense channel did not return are fetched in
        # **one** query. Fetching them one by one is an N+1: each vector
        # search scans the whole table (0.2 s at 58k rows), and a query
        # with 30 full-text hits would spend six seconds there - four
        # times the budget for the whole retrieval (AC-P-002).
        missing = [cid for cid, _ in fts_hits if cid not in dense_ids]
        fetched: dict[str, dict[str, Any]] = {}
        if missing:
            ids_clause = ", ".join(f"'{cid}'" for cid in missing)

            def _fetch_missing() -> list[dict[str, Any]]:
                where = f"id IN ({ids_clause})"
                if filter_expr:
                    where = f"({filter_expr}) AND {where}"
                return table.search(dense_q).where(where).limit(len(missing)).to_list()

            try:
                rows = await asyncio.wait_for(
                    asyncio.to_thread(_fetch_missing), timeout=self.dense_timeout
                )
            except Exception:
                rows = []
            for row in rows:
                # These rows come from a vector search whose distance is
                # meaningless - the row was picked by its id.
                row.pop("_distance", None)
                row["_score_dense"] = 0.0
                fetched[row["id"]] = row

        for cid, fts_score in fts_hits:
            base = dense_ids.get(cid)
            if base is None:
                base = fetched.get(cid)
            if base is None:
                continue
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
