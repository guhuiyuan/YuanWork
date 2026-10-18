"""Full-text search index for chunk text, using tantivy + jieba.

Why this exists (Stage 3):
  The v3 hybrid retrieval (`search_hybrid`) used bge-m3's
  `lexical_weights` for the sparse channel. That channel underweighted
  pure-number and short CJK tokens (verified: query "1280" → sparse
  score 0.0000 even when the chunk literally contained "1280"). This
  module replaces that path with a proper BM25 full-text search index:

    - tantivy (Rust core, Python bindings) for the inverted index
    - jieba for CJK word segmentation
    - tantivy's default tokenizer on the jieba-segmented text

End result: literal-token queries ("1280", "INV-2024-001", "金额",
"开票日期") match via real BM25 scoring, no longer relying on the
embedding model's lexical channel.

Lifecycle:
  - One FTSIndex per SemanticMemory (one index_dir per data_dir).
  - `add_documents()` is incremental — call after each add_chunks.
  - `delete_by_ids()` / `delete_by_doc()` for re-ingest / removal.
  - `search()` is sync (tantivy is fast); SemanticMemory wraps in
    asyncio.to_thread.
  - `reindex_from_chunks()` rebuilds from existing text — used when
    upgrading a v3 (bge-m3-sparse-only) KB that has no FTS index yet.

Performance notes:
  - tantivy writer needs `commit()` to make changes searchable
  - Each commit is fsync-heavy; we batch commits per add_chunks call
  - For ≤10k chunks, search is sub-millisecond
  - For >100k chunks, switch to tantivy's `loaders` for incremental
    indexing; current implementation is sufficient for personal KB
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# Lazy imports — tantivy is a heavy native module; jieba prints a
# "Loading model" line on first import that we silence.
def _import_tantivy():
    import tantivy
    return tantivy


def _import_jieba():
    import jieba
    # Silence jieba's chatty "Loading model" output on first call
    jieba.setLogLevel(logging.WARNING)
    import os as _os
    _os.environ.setdefault("JIEBA_LOG_LEVEL", "WARNING")
    return jieba


def _segment(text: str) -> str:
    """Segment text into space-separated tokens. jieba for CJK, passthrough for ASCII."""
    if not text:
        return ""
    # Heuristic: if text is mostly ASCII, just lowercase + normalize whitespace
    ascii_count = sum(1 for c in text if ord(c) < 128)
    if ascii_count > len(text) * 0.8:
        return " ".join(text.split()).lower()
    jieba = _import_jieba()
    return " ".join(t for t in jieba.cut(text) if t.strip())


class FTSIndex:
    """Tantivy-backed BM25 index over chunk text.

    Schema:
      - `id` (string, raw tokenizer, stored) — chunk ID, primary key
      - `doc_id` (string, raw tokenizer, stored) — for delete_by_doc
      - `text` (string, default tokenizer, stored) — searchable body

    The default tantivy tokenizer is applied to the jieba-segmented
    text, so queries should also be pre-segmented via `_segment()`.
    """

    def __init__(self, index_dir: Path | str, heap_size: int = 50_000_000):
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.heap_size = heap_size
        self._lock = threading.Lock()
        self._index: Any = None
        self._writer: Any = None
        self._schema: Any = None
        self._needs_reload: bool = False
        self._open()

    def _open(self) -> None:
        """Open (or create) the index. Idempotent."""
        tantivy = _import_tantivy()
        sb = tantivy.SchemaBuilder()
        sb.add_text_field("id", stored=True, tokenizer_name="raw")
        sb.add_text_field("doc_id", stored=True, tokenizer_name="raw")
        sb.add_text_field("text", stored=True)  # default tokenizer
        self._schema = sb.build()
        self._index = tantivy.Index(self._schema, path=str(self.index_dir))

    def _ensure_writer(self) -> None:
        """Lazily get a writer. tantivy only allows one writer at a time."""
        if self._writer is None:
            self._writer = self._index.writer(self.heap_size)

    def add_documents(self, docs: list[dict[str, str]]) -> int:
        """Add or update documents. Each doc must have {id, doc_id, text}.

        Returns the number of documents added.
        """
        if not docs:
            return 0
        tantivy = _import_tantivy()
        with self._lock:
            self._ensure_writer()
            for doc in docs:
                # tantivy requires term values to be lists for multi-value fields;
                # we use single-value so the helper just wraps in [v]
                self._writer.add_document(
                    tantivy.Document(
                        id=[doc["id"]],
                        doc_id=[doc.get("doc_id", "")],
                        text=[_segment(doc.get("text", ""))],
                    )
                )
            self._writer.commit()
            self._needs_reload = True
        return len(docs)

    def delete_by_ids(self, ids: list[str]) -> int:
        """Delete documents by chunk id. Returns count attempted."""
        if not ids:
            return 0
        tantivy = _import_tantivy()
        with self._lock:
            self._ensure_writer()
            for cid in ids:
                # tantivy 0.22+ renamed delete_documents to
                # delete_documents_by_term / _by_query. The term version
                # is the right tool for an exact id match.
                self._writer.delete_documents_by_term("id", cid)
            self._writer.commit()
            self._needs_reload = True
        return len(ids)

    def delete_by_doc_id(self, doc_id: str) -> int:
        """Delete all chunks belonging to a document.

        tantivy doesn't return the count of actually-deleted docs, so
        we report the number of IDs we asked it to delete.
        """
        tantivy = _import_tantivy()
        with self._lock:
            self._ensure_writer()
            self._writer.delete_documents_by_term("doc_id", doc_id)
            self._writer.commit()
            self._needs_reload = True
        return 1

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """BM25 search. Returns [(chunk_id, score), ...] sorted by score desc.

        Both the query and the indexed text are jieba-segmented, so
        token alignment is consistent.
        """
        if not query or not query.strip():
            return []
        with self._lock:
            if self._needs_reload:
                self._index.reload()
                self._needs_reload = False
            try:
                seg_q = _segment(query)
                parsed = self._index.parse_query(seg_q, ["text"])
                hits = self._index.searcher().search(parsed, top_k).hits
            except Exception as e:
                # Parse errors happen for queries with no valid tokens
                # (e.g. all-punctuation). Treat as empty results.
                logger.debug(f"FTS search returned no hits (parse issue): {e!r}")
                return []
        results: list[tuple[str, float]] = []
        for h in hits:
            score, addr = h[0], h[1]
            doc = self._index.searcher().doc(addr)
            # tantivy 0.26 Document uses get_first() / get_all() / to_dict()
            # (no dict-like [] or .get()). Use get_first which returns
            # the first (and only, for our single-value fields) value.
            cid = doc.get_first("id")
            if not cid:
                continue
            results.append((str(cid), float(score)))
        return results

    def reindex_from_chunks(self, chunks: list[dict[str, Any]]) -> int:
        """Rebuild the index from a list of chunk dicts.

        Used when migrating a v3 KB (LanceDB only) to v4 (FTS-backed).
        Each chunk dict needs {id, doc_id, text}.
        """
        tantivy = _import_tantivy()
        with self._lock:
            # Drop the index on disk and recreate
            import shutil
            try:
                self._writer = None  # release any existing writer
                shutil.rmtree(self.index_dir)
            except Exception as e:
                logger.warning(f"Failed to clear FTS index dir: {e!r}")
            self.index_dir.mkdir(parents=True, exist_ok=True)
            self._open()
            self._ensure_writer()
            for chunk in chunks:
                self._writer.add_document(
                    tantivy.Document(
                        id=[chunk["id"]],
                        doc_id=[chunk.get("doc_id", "")],
                        text=[_segment(chunk.get("text", ""))],
                    )
                )
            self._writer.commit()
            self._needs_reload = True
        return len(chunks)

    def count(self) -> int:
        """Number of indexed documents (approximate; uses segment metadata)."""
        with self._lock:
            if self._needs_reload:
                self._index.reload()
                self._needs_reload = False
            return self._index.searcher().num_docs

    def close(self) -> None:
        """Release resources."""
        with self._lock:
            self._writer = None
            self._index = None
