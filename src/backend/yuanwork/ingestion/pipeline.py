"""Ingestion pipeline - orchestrates parse → chunk → embed → index."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from yuanwork.ingestion.chunker.structural_chunker import StructuralChunker
from yuanwork.ingestion.chunker.token_counter import TokenCounter, content_token_limit
from yuanwork.ingestion.parser.dispatcher import ParserDispatcher
from yuanwork.workspace import Workspace

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.memory.semantic import SemanticMemory


logger = logging.getLogger(__name__)

#: Files above this size are skipped instead of being read into memory.
DEFAULT_MAX_FILE_SIZE = 100 * 1024 * 1024

#: How long one core document may take to compile (AC-P-008).
DEFAULT_CORE_COMPILE_TIMEOUT = 240.0

#: How many slices are written to the store at once (AC-P-007).
DEFAULT_INDEX_BATCH_SIZE = 256

#: How many slices a second may be written at most. Zero = no cap.
DEFAULT_INDEX_RATE_LIMIT = 0.0


def default_core_compile_timeout() -> float:
    """How long a compilation may take, from the settings."""
    try:
        from yuanwork.config import get_settings

        settings = get_settings()
        return float(settings.core_compile_timeout_seconds)
    except Exception:  # noqa: BLE001 - the default is enough
        return DEFAULT_CORE_COMPILE_TIMEOUT


def default_index_batch_size() -> int:
    """How many slices are written at once, from the settings."""
    try:
        from yuanwork.config import get_settings

        settings = get_settings()
        return int(settings.index_batch_size)
    except Exception:  # noqa: BLE001 - the default is enough
        return DEFAULT_INDEX_BATCH_SIZE


def default_index_rate_limit() -> float:
    """How many slices a second may be written at most."""
    try:
        from yuanwork.config import get_settings

        settings = get_settings()
        return float(settings.index_rate_limit)
    except Exception:  # noqa: BLE001 - the default is enough
        return DEFAULT_INDEX_RATE_LIMIT


class ChunkBatch:
    """The slices waiting to be written.

    A build of thousands of files is written in batches: one embedding
    call per batch instead of one per file, and one write to the store
    instead of thousands. The writer also keeps the average rate under
    a cap, so a build does not starve the rest of the machine
    (AC-P-007).
    """

    def __init__(
        self,
        semantic: Any,
        batch_size: int = 256,
        rate_limit: float = 0.0,
    ):
        self.semantic = semantic
        self.batch_size = max(1, int(batch_size))
        #: How many slices a second may be written at most. Zero = no cap.
        self.rate_limit = max(0.0, float(rate_limit))
        self.pending: list[dict] = []
        self.written = 0
        self.batches = 0
        #: How long the embedding and the writing took, in seconds.
        self.timings: dict[str, float] = {"embed": 0.0, "write": 0.0}

    async def add(self, chunks: list[dict]) -> None:
        """Take the slices of one file, and write them when the batch is
        full."""
        self.pending.extend(chunks)
        if len(self.pending) >= self.batch_size:
            await self.flush()

    async def flush(self) -> None:
        """Write what is waiting."""
        if not self.pending:
            return
        pending, self.pending = self.pending, []
        started = time.perf_counter()
        try:
            await self.semantic.add_chunks(pending)
            self.written += len(pending)
            self.batches += 1
            stage = getattr(self.semantic, "last_timings", None) or {}
            self.timings["embed"] += stage.get("embed", 0.0)
            self.timings["write"] += stage.get("write", 0.0)
        except Exception as e:  # noqa: BLE001 - one batch is not the whole build
            logger.warning(f"A batch of {len(pending)} slices failed: {e}")
            return

        if self.rate_limit > 0:
            # Keep the average rate under the cap.
            expected = len(pending) / self.rate_limit
            elapsed = time.perf_counter() - started
            if expected > elapsed:
                await asyncio.sleep(expected - elapsed)


class IngestionPipeline:
    """Orchestrates the full document ingestion workflow."""

    def __init__(
        self,
        sqlite_store: "SQLiteStore",
        semantic_memory: "SemanticMemory",
        max_file_size: int = DEFAULT_MAX_FILE_SIZE,
        llm_gateway: Any | None = None,
        core_compile_timeout: float | None = None,
        index_batch_size: int | None = None,
        index_rate_limit: float | None = None,
    ):
        self.sqlite = sqlite_store
        self.semantic = semantic_memory
        self.dispatcher = ParserDispatcher()
        # The chunk length is the embedding model's read length: the two
        # use one number, so they cannot disagree. A chunk the model
        # cannot read in full is a chunk whose tail nobody can find
        # again (ADR-046).
        self.chunker = StructuralChunker(
            max_tokens=content_token_limit(
                getattr(semantic_memory, "embedding_max_tokens", 512)
            ),
            token_counter=TokenCounter(
                getattr(semantic_memory, "embedding_model_name", None)
            ),
        )
        self.workspace = Workspace(sqlite_store)
        self.max_file_size = max_file_size
        #: The documents the vector table really holds. Read once, when
        #: a build asks whether a file is in the index (TASK-088).
        self._indexed_doc_ids: set[str] | None = None
        # The tier judgement may ask the model about the documents the
        # rules cannot place (ADR-031).
        self.llm_gateway = llm_gateway
        # The core layer is compiled while the document is indexed
        # (ADR-031) - never while a question is being answered.
        self.core_compile_timeout = (
            core_compile_timeout if core_compile_timeout is not None
            else default_core_compile_timeout()
        )
        # How a bulk build writes: in batches, at a capped rate
        # (AC-P-007).
        self.index_batch_size = (
            index_batch_size if index_batch_size is not None
            else default_index_batch_size()
        )
        self.index_rate_limit = (
            index_rate_limit if index_rate_limit is not None
            else default_index_rate_limit()
        )
        # The writer the bulk build uses. A single file goes straight
        # to the store; a build writes in batches (AC-P-007).
        self._batch: "ChunkBatch | None" = None
        # How long each stage took, in seconds. The benchmark reads
        # them to say where the time of a build went (AC-P-010/011/012).
        self.timings: dict[str, float] = {
            "parse": 0.0,
            "chunk": 0.0,
            "embed": 0.0,
            "write": 0.0,
            "compile": 0.0,
        }

    @classmethod
    def from_app(cls, app) -> "IngestionPipeline":
        """Create pipeline from FastAPI app state."""
        from yuanwork.api.deps import (
            get_llm_gateway,
            get_semantic_memory,
            get_sqlite_store,
        )

        max_file_size = DEFAULT_MAX_FILE_SIZE
        settings = getattr(app.state, "settings", None)
        if settings is not None:
            max_file_size = int(getattr(settings, "max_file_size_mb", 100)) * 1024 * 1024

        # The tier judgement may ask the model - but a build without a
        # model is still a build.
        try:
            llm_gateway = get_llm_gateway()
        except RuntimeError:
            llm_gateway = None

        return cls(
            sqlite_store=get_sqlite_store(),
            semantic_memory=get_semantic_memory(),
            max_file_size=max_file_size,
            llm_gateway=llm_gateway,
        )

    async def ingest_file(
        self,
        file_path: Path,
        force: bool = False,
        batch: "ChunkBatch | None" = None,
    ) -> dict:
        """Ingest a single file.

        Args:
            file_path: Path to file
            force: Re-ingest even if hash matches existing record
            batch: The writer the slices go into. A bulk build writes
                in batches instead of one file at a time (AC-P-007).

        Returns:
            Dict with status and stats
        """
        file_path = Path(file_path)
        if not file_path.exists():
            return {"status": "error", "error": "File not found", "path": str(file_path)}

        # Get parser
        parser = self.dispatcher.get_parser(file_path)
        if parser is None:
            return {
                "status": "skipped",
                "error": f"No parser for extension: {file_path.suffix}",
                "path": str(file_path),
            }

        # Compute hash
        file_hash = self._hash_file(file_path)

        # The resume: what the index already knows about this file
        # decides whether it is read at all.
        stale_doc_id: str | None = None
        if not force:
            state = await self.resume_state(file_path, file_hash)
            if state["state"] == "already_indexed":
                logger.info(f"Skipping {file_path.name} - already indexed")
                return {
                    "status": "skipped",
                    "reason": "already_indexed",
                    "doc_id": state["doc_id"],
                }
            if state["state"] == "stale":
                # The content is in the index, but it was cut with the
                # old rules - and the old rules cut off the tail of the
                # long ones. It is cut again, under the same id, so the
                # old chunks are replaced rather than left behind
                # (ADR-046).
                stale_doc_id = state["doc_id"]
                logger.info(
                    "Re-cutting %s - it was cut with the old rules", file_path.name
                )
            if state["state"] == "modified":
                # The file changed on disk. The indexed version is kept as
                # it is until the user asks for a rebuild - silently
                # replacing it would take the old version out of the index
                # without being asked (AC-E-015).
                logger.info(f"Skipping {file_path.name} - content changed")
                return {
                    "status": "skipped",
                    "reason": "modified",
                    "doc_id": state["doc_id"],
                    "stale": True,
                }

        # Generate doc_id
        doc_id = hashlib.sha256(
            f"{file_path}|{file_hash}".encode()
        ).hexdigest()[:16]
        if stale_doc_id:
            doc_id = stale_doc_id

        # An explicit rebuild replaces the indexed version of this file -
        # otherwise the old one would stay searchable next to the new one.
        if force:
            await self.drop_previous_version(file_path, doc_id)

        try:
            # Parse
            started = time.perf_counter()
            blocks = await parser.parse(file_path, doc_id)
            self._record("parse", time.perf_counter() - started)

            return await self.index_blocks(
                file_path, doc_id, file_hash, blocks, batch=batch
            )

        except Exception as e:
            logger.exception(f"Failed to ingest {file_path}: {e}")
            return {"status": "error", "error": str(e), "path": str(file_path)}

    async def index_blocks(
        self,
        file_path: Path,
        doc_id: str,
        file_hash: str,
        blocks: list,
        batch: "ChunkBatch | None" = None,
        compile_now: bool = True,
    ) -> dict:
        """What one parsed document is worth: slices, a file record, pages.

        The reading and the writing are separate: the bulk build parses in
        a worker process and calls this here, while the one-file route
        parses and calls this too. Both write the same way (TASK-045).
        """
        file_path = Path(file_path)
        if not blocks:
            return {"status": "skipped", "reason": "no_content", "doc_id": doc_id}

        # Chunk
        # In a thread: the first chunk of a build loads the tokenizer,
        # and that means importing transformers - which takes seconds
        # and holds the GIL. In the event loop that would stall the
        # whole backend; in a thread it only makes it slower (ADR-046).
        started = time.perf_counter()
        chunks = await asyncio.to_thread(self.chunker.chunk, blocks, doc_id)
        self._record("chunk", time.perf_counter() - started)

        # Add metadata from parser
        meta = await self.metadata_of(file_path)

        # Add filename/path to chunks
        for chunk in chunks:
            chunk["file_name"] = file_path.name
            chunk["file_path"] = str(file_path)
            chunk["file_type"] = file_path.suffix.lower().lstrip(".")
            chunk["meta"].setdefault("file_meta", meta)

        # Which layer the document belongs to (ADR-031). A judgement
        # that fails must not stop the build - the document goes to
        # the long tail and the build goes on.
        tier = await self._classify(doc_id, file_path, file_hash, chunks)

        # Embed and index. A bulk build writes in batches: one
        # embedding call per batch instead of one per file.
        writer = batch if batch is not None else getattr(self, "_batch", None)
        if writer is not None:
            await writer.add(chunks)
        else:
            await self.semantic.add_chunks(chunks)
            # A single file is indexed on its own - there is no build
            # to end with a flush. The full-text index keeps the
            # documents in its hands until it decides to flush them
            # (ADR-043), so this one has to say "now they are
            # searchable" - otherwise the file would be in the vector
            # store and missing from the text search.
            flush_fts = getattr(self.semantic, "flush_fts", None)
            if flush_fts is not None:
                await flush_fts()

        # Update SQLite record
        await self.upsert_file(doc_id, file_path, file_hash, len(chunks), meta)

        # The core layer is compiled while the document is indexed
        # (ADR-031) - the pages are there before anybody asks. The
        # compilation is not on the way a question is answered, and
        # one that fails does not stop the build. A bulk build compiles
        # in the background instead - the build is finished when the
        # long tail is searchable (AC-F-026).
        compiled: dict = {"compiled": False}
        if compile_now:
            started = time.perf_counter()
            compiled = await self._compile_if_core(
                doc_id, file_path, blocks, tier, file_hash
            )
            self._record("compile", time.perf_counter() - started)

        logger.info(
            f"Indexed {file_path.name}: {len(chunks)} chunks "
            f"from {len(blocks)} blocks"
        )

        return {
            "status": "indexed",
            "doc_id": doc_id,
            "blocks": len(blocks),
            "chunks": len(chunks),
            "tier": tier.get("tier"),
            "decided_by": tier.get("decided_by"),
            "compiled": compiled.get("compiled", False),
            "pages": len(compiled.get("pages") or []),
        }

    async def drop_previous_version(
        self, file_path: Path, doc_id: str
    ) -> None:
        """Take the indexed version of this file out of the index.

        An explicit rebuild replaces it - otherwise the old one would
        stay searchable next to the new one.
        """
        previous = await self._indexed_version_at(file_path)
        if previous is None or previous["doc_id"] == doc_id:
            return
        # The old record is history: the file it was read from has
        # changed (AC-E-015).
        await self._mark_stale(previous["doc_id"])
        await self.semantic.delete_by_doc_id(previous["doc_id"])
        await self.sqlite.delete_file(previous["doc_id"])

    async def build_directory(
        self,
        dir_path: Path,
        recursive: bool = True,
        force: bool = False,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """The bulk build: many files, read in parallel, written in batches.

        The files are read in a few worker processes, the slices are
        written in batches at a capped rate, and the core layer's pages
        are compiled in the background - the build is finished when the
        long tail is searchable (AC-F-026, TASK-045).
        """
        from yuanwork.ingestion.batch import BatchIngestor

        ingestor = BatchIngestor(self)
        return await ingestor.ingest(
            dir_path, recursive=recursive, force=force, on_progress=on_progress
        )

    async def ingest_directory(
        self,
        dir_path: Path,
        recursive: bool = True,
        force: bool = False,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """Ingest all supported files in a directory, in this process.

        One file at a time - the plain build. The bulk build is
        `build_directory`: it reads in parallel and writes in batches
        (TASK-045).

        `on_progress(done, total, current_path)` is called after every
        file, so a caller can show live progress instead of a spinner
        that never moves.
        """
        dir_path = Path(dir_path)
        if not dir_path.exists() or not dir_path.is_dir():
            return {"status": "error", "error": "Directory not found"}

        files = self.supported_files(dir_path, recursive)

        results = {"indexed": 0, "skipped": 0, "errors": 0, "files": []}
        total = len(files)

        # The slices are written in batches: one embedding call per
        # batch instead of one per file (AC-P-007).
        batch = ChunkBatch(
            self.semantic,
            batch_size=getattr(self, "index_batch_size", DEFAULT_INDEX_BATCH_SIZE),
            rate_limit=getattr(self, "index_rate_limit", DEFAULT_INDEX_RATE_LIMIT),
        )
        self._batch = batch

        for done, file_path in enumerate(files, start=1):
            result = await self.ingest_file(file_path, force=force, batch=batch)
            results["files"].append({"path": str(file_path), **result})

            if result["status"] == "indexed":
                results["indexed"] += 1
            elif result["status"] == "skipped":
                results["skipped"] += 1
            else:
                results["errors"] += 1

            await self._report_progress(on_progress, done, total, file_path)

        # Whatever is left in the batch goes in too.
        await batch.flush()
        self._batch = None
        self._record("embed", batch.timings.get("embed", 0.0))
        self._record("write", batch.timings.get("write", 0.0))

        return results

    def supported_files(self, dir_path: Path, recursive: bool = True) -> list[Path]:
        """The files in a directory that this pipeline would actually index.

        Exposed so callers can report the real total up front instead of
        guessing from every file in the directory - the count of
        unsupported files would otherwise make the progress bar lie.
        """
        dir_path = Path(dir_path)
        supported = self.dispatcher.supported_extensions()
        if recursive:
            files = [f for f in dir_path.rglob("*") if f.is_file()]
        else:
            files = [f for f in dir_path.glob("*") if f.is_file()]
        return [f for f in files if f.suffix.lower().lstrip(".") in supported]

    def partition_workspace_files(
        self, files: list[Path]
    ) -> tuple[list[Path], list[dict], list[dict]]:
        """Sort the workspace's files into: to read, not supported, too big.

        The three groups are what the build makes of a workspace, and
        they are told apart the same way everywhere - the job that says
        how much work there is, and the build that does the work.
        """
        supported = set(self.dispatcher.supported_extensions())

        ready: list[Path] = []
        unsupported: list[dict] = []
        oversized: list[dict] = []
        for file_path in files:
            suffix = file_path.suffix.lower().lstrip(".")
            if suffix not in supported:
                unsupported.append(
                    {
                        "path": str(file_path),
                        "reason": f"不支持的文件类型：{file_path.suffix or '（无扩展名）'}",
                    }
                )
                continue

            size = self._file_size(file_path)
            if size is not None and self.max_file_size and size > self.max_file_size:
                limit_mb = max(1, self.max_file_size // (1024 * 1024))
                oversized.append(
                    {
                        "path": str(file_path),
                        "status": "skipped",
                        "reason": f"文件超过体积上限（{limit_mb} MB），已跳过",
                    }
                )
                continue

            ready.append(file_path)

        return ready, unsupported, oversized

    async def workspace_build_size(self) -> int:
        """How many files the workspace build will actually work on.

        The workspace covers far more files than can be indexed - a
        code project is mostly source. The progress bar and the job
        count the work, not the files that are merely there.
        """
        files = await self.workspace.list_files(included_only=True)
        ready, _unsupported, oversized = self.partition_workspace_files(files)
        return len(ready) + len(oversized)

    async def build_workspace(
        self,
        force: bool = False,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """Build the knowledge base from the workspace, in parallel.

        The same build a folder gets - the files are read in worker
        processes, the slices are written in batches - over the files
        the workspace covers: the folders it holds, expanded, plus the
        files added by hand, each counted once (TASK-051).

        The files that cannot be indexed are reported with the reason -
        an unsupported file type (AC-E-004) and a file above the size
        limit (AC-E-006) are never dropped silently. A file that is
        already indexed is skipped: the build can be resumed.
        """
        from yuanwork.ingestion.batch import BatchIngestor

        entries = await self.workspace.list_entries(included_only=True)
        files = await self.workspace.list_files(included_only=True)
        ready, unsupported, oversized = self.partition_workspace_files(files)

        # The progress bar counts the work, not the workspace. A
        # workspace is mostly files nobody can index - a code project,
        # an archive - and counting them would put the bar at 97%
        # before the first document is read. The files that are only
        # reported (too big) count as done: the build will not come
        # back to them.
        total = len(ready) + len(oversized)
        reported = len(oversized)

        async def _progress(done: int, _total: int, current: str) -> None:
            await self._report_progress(
                on_progress, reported + done, total, Path(current)
            )

        if oversized:
            # The files that are reported are done: the build will not
            # come back to them.
            await self._report_progress(
                on_progress, reported, total, Path(oversized[0]["path"])
            )

        ingestor = BatchIngestor(self)
        result = await ingestor.ingest(
            files=ready, force=force, on_progress=_progress
        )

        # The single files the user added by hand remember which
        # document they were indexed under.
        for item in result.get("files", []):
            if item.get("status") != "indexed":
                continue
            await self._record_entry_doc_id(entries, Path(item["path"]), item)

        return {
            "status": "completed",
            "indexed": result.get("indexed", 0),
            "skipped": result.get("skipped", 0) + len(oversized),
            "errors": result.get("errors", 0),
            "unsupported": len(unsupported),
            "files": [*oversized, *result.get("files", [])],
            "unsupported_files": unsupported,
            "compiled_in_background": result.get("compiled_in_background", 0),
            "core_pending": result.get("core_pending", 0),
        }

    async def ingest_workspace(
        self,
        force: bool = False,
        on_progress: Callable[[int, int, str], None] | None = None,
    ) -> dict:
        """Ingest every file the workspace covers.

        The workspace is the scope of the knowledge base: a folder plus the
        files added by hand. Files that cannot be indexed are reported with
        the reason - they are never dropped silently.
        """
        entries = await self.workspace.list_entries(included_only=True)
        files = await self.workspace.list_files(included_only=True)
        supported = set(self.dispatcher.supported_extensions())

        results: dict = {
            "status": "completed",
            "indexed": 0,
            "skipped": 0,
            "errors": 0,
            "unsupported": 0,
            "files": [],
            "unsupported_files": [],
        }
        total = len(files)

        for done, file_path in enumerate(files, start=1):
            suffix = file_path.suffix.lower().lstrip(".")
            if suffix not in supported:
                results["unsupported"] += 1
                results["unsupported_files"].append({
                    "path": str(file_path),
                    "reason": f"不支持的文件类型：{file_path.suffix or '（无扩展名）'}",
                })
                await self._report_progress(on_progress, done, total, file_path)
                continue

            size = self._file_size(file_path)
            if size is not None and self.max_file_size and size > self.max_file_size:
                limit_mb = max(1, self.max_file_size // (1024 * 1024))
                results["skipped"] += 1
                results["files"].append({
                    "path": str(file_path),
                    "status": "skipped",
                    "reason": f"文件超过体积上限（{limit_mb} MB），已跳过",
                })
                await self._report_progress(on_progress, done, total, file_path)
                continue

            result = await self.ingest_file(file_path, force=force)
            results["files"].append({"path": str(file_path), **result})

            if result["status"] == "indexed":
                results["indexed"] += 1
                await self._record_entry_doc_id(entries, file_path, result)
            elif result["status"] == "skipped":
                results["skipped"] += 1
            else:
                results["errors"] += 1

            await self._report_progress(on_progress, done, total, file_path)

        return results

    async def _classify(
        self,
        doc_id: str,
        file_path: Path,
        file_hash: str,
        chunks: list[dict],
    ) -> dict:
        """Decide which layer this document belongs to (ADR-031).

        A judgement that fails does not stop the build: the document
        goes to the long tail and the reason is written down.
        """
        from yuanwork.ingestion.tier import TierClassifier

        classifier = TierClassifier(self.sqlite, llm_gateway=self.llm_gateway)
        text_length = sum(len(str(chunk.get("text") or "")) for chunk in chunks)
        head = str(chunks[0].get("text") or "")[:600] if chunks else ""

        try:
            return await classifier.classify(
                {
                    "doc_id": doc_id,
                    "file_path": str(file_path),
                    "file_name": file_path.name,
                    "file_type": file_path.suffix.lower().lstrip("."),
                    "file_hash": file_hash,
                    "text_length": text_length,
                    "head": head,
                }
            )
        except Exception as e:  # noqa: BLE001 - a failed judgement is not a failed build
            logger.warning(f"The tier judgement of {file_path.name} failed: {e}")
            return {"tier": "longtail", "decided_by": "rule"}

    async def _compile_if_core(
        self,
        doc_id: str,
        file_path: Path,
        blocks: list,
        tier: dict,
        file_hash: str = "",
    ) -> dict:
        """Compile a core document into pages (ADR-031, AC-F-022).

        A long-tail document is not compiled: it is sliced and searched
        when a question comes in. A compilation that fails does not stop
        the build - the file is marked and the rest of the build goes on
        (AC-E-014, TASK-038).
        """
        if str(tier.get("tier") or "") != "core":
            return {"compiled": False, "reason": "长尾层文档不编译"}

        from yuanwork.wiki.core_compiler import CoreCompiler

        compiler = CoreCompiler(
            self.sqlite,
            llm_gateway=self.llm_gateway,
            timeout=self.core_compile_timeout,
        )
        text = "\n\n".join(
            str(getattr(block, "content", "") or "") for block in blocks
        ).strip()

        try:
            result = await compiler.compile(
                {
                    "doc_id": doc_id,
                    "file_name": file_path.name,
                    "file_path": str(file_path),
                    "source_hash": file_hash,
                    "text": text,
                    # The pieces of the document, each with the place it
                    # was parsed from - a conclusion read from them can
                    # say where it came from (ADR-032).
                    "segments": self._segments_of(blocks),
                }
            )
        except Exception as e:  # noqa: BLE001 - a failed compile is not a failed build
            logger.warning(f"The compilation of {file_path.name} failed: {e}")
            return {"compiled": False, "error": str(e)}

        logger.info(
            f"Compiled {file_path.name}: {len(result.get('pages') or [])} pages"
        )
        return result

    @staticmethod
    def _segments_of(blocks: list) -> list[dict]:
        """The pieces of a document, each with where it was parsed from."""
        segments: list[dict] = []
        for block in blocks:
            content = str(getattr(block, "content", "") or "")
            if not content.strip():
                continue
            meta = getattr(block, "meta", None)
            meta = meta if isinstance(meta, dict) else {}
            anchor = meta.get("anchor")
            segments.append(
                {
                    "text": content,
                    "anchor": anchor if isinstance(anchor, dict) and anchor else None,
                }
            )
        return segments

    # --------------------------------------------------------------------------
    # the pieces the batch ingestor uses
    # --------------------------------------------------------------------------

    def hash_file(self, file_path: Path) -> str:
        """The hash of a file's contents."""
        return self._hash_file(file_path)

    async def resume_state(self, file_path: Path, file_hash: str) -> dict[str, Any]:
        """What the index already knows about this file.

        * ``already_indexed`` - the very same content is in the index,
          cut with the rules that are in use now. A build skips it and
          goes on (the resume).
        * ``stale`` - the content is in the index, but it was cut with
          **other rules**. It is cut again (ADR-046).
        * ``modified`` - the file changed on disk. The indexed version
          is kept and marked; it is not replaced without being asked
          (AC-E-015).
        * ``new`` - nothing is known about it.

        The ledger alone does not decide: a build that was interrupted
        after the ledger was written and before the slices were flushed
        leaves files that are "indexed" with nothing in the index. Those
        files are not searchable - and, trusting the ledger, they could
        not be repaired either: the build skipped them (TASK-088).
        """
        same = await self.sqlite.get_file_by_hash(file_hash)
        if same:
            if same.get("chunk_version") == self.chunker.version:
                if await self._index_holds(same.get("doc_id")):
                    return {"state": "already_indexed", "doc_id": same.get("doc_id")}
                # The ledger says it is in the index; the index says it
                # is not. The index is the truth - so the file is read
                # again.
                logger.info(
                    f"Re-indexing {file_path.name} - the ledger has it, "
                    f"the index does not"
                )
                return {"state": "new", "doc_id": None}
            return {"state": "stale", "doc_id": same.get("doc_id")}

        previous = await self._indexed_version_at(file_path)
        if previous is not None:
            await self._mark_stale(previous["doc_id"])
            return {"state": "modified", "doc_id": previous.get("doc_id")}

        return {"state": "new", "doc_id": None}

    async def _index_holds(self, doc_id: str | None) -> bool:
        """Whether the vector table really holds this document.

        The set of ids in the index is read once and kept: a build asks
        the question for every file it walks, and reading the table
        once costs a fraction of a second (TASK-088).

        A store that cannot answer - a stand-in in the tests, or an
        index that cannot be read - leaves the ledger in charge: the
        old behaviour is the safe one there.
        """
        if not doc_id:
            return False
        if self._indexed_doc_ids is None:
            reader = getattr(self.semantic, "indexed_doc_ids", None)
            if reader is None:
                return True
            try:
                self._indexed_doc_ids = await reader()
            except Exception as e:  # noqa: BLE001 - the ledger is the fallback
                logger.warning(f"The index's document ids could not be read: {e}")
                return True
        return doc_id in self._indexed_doc_ids

    async def metadata_of(self, file_path: Path) -> dict[str, Any]:
        """The metadata the parser knows about a file."""
        parser = self.dispatcher.get_parser(file_path)
        if parser is None:
            return {}
        return await parser.extract_metadata(file_path)

    async def classify(
        self,
        doc_id: str,
        file_path: Path,
        file_hash: str,
        chunks: list[dict],
    ) -> dict[str, Any]:
        """Which layer a document belongs to."""
        return await self._classify(doc_id, file_path, file_hash, chunks)

    async def write_chunks(self, chunks: list[dict]) -> None:
        """Write the slices of one document.

        A bulk build writes in batches: one embedding call per batch
        instead of one per file.
        """
        if self._batch is not None:
            await self._batch.add(chunks)
        else:
            await self.semantic.add_chunks(chunks)
        # The index has a new document in it (TASK-088).
        self._indexed_doc_ids = None

    async def upsert_file(
        self,
        doc_id: str,
        file_path: Path,
        file_hash: str,
        chunk_count: int,
        meta: dict[str, Any],
    ) -> None:
        """Write the file's record."""
        await self.sqlite.upsert_file(
            doc_id=doc_id,
            file_name=file_path.name,
            file_path=str(file_path),
            file_hash=file_hash,
            file_type=file_path.suffix.lower().lstrip("."),
            size_bytes=file_path.stat().st_size,
            chunk_count=chunk_count,
            # Which rules the file was cut with - so that a change of
            # the rules cuts it again instead of skipping it (ADR-046).
            chunk_version=self.chunker.version,
            meta=meta,
        )

    async def compile_document(
        self,
        doc_id: str,
        file_path: Path,
        blocks: list,
        tier: dict,
        file_hash: str = "",
    ) -> dict[str, Any]:
        """Compile a core document into pages."""
        return await self._compile_if_core(doc_id, file_path, blocks, tier, file_hash)

    async def flush_batch(self) -> None:
        """Write whatever the batch still holds."""
        if self._batch is not None:
            await self._batch.flush()
        # The index has new documents in it - the set of ids read for
        # the resume has to be read again (TASK-088).
        self._indexed_doc_ids = None

    def _record(self, stage: str, seconds: float) -> None:
        """How long a stage took.

        A pipeline built by hand (the tests do that) has no timings
        yet - it gets them on the first call.
        """
        timings = getattr(self, "timings", None)
        if timings is None:
            timings = {
                "parse": 0.0,
                "chunk": 0.0,
                "embed": 0.0,
                "write": 0.0,
                "compile": 0.0,
            }
            self.timings = timings
        timings[stage] = timings.get(stage, 0.0) + seconds

    async def _indexed_version_at(self, file_path: Path) -> dict | None:
        """The version of this file that is currently indexed, if any."""
        lookup = getattr(self.sqlite, "get_file_by_path", None)
        if lookup is None:
            return None
        try:
            return await lookup(str(file_path))
        except Exception:  # noqa: BLE001 - a lookup failure must not block ingest
            logger.exception("Path lookup failed for %s", file_path)
            return None

    async def _mark_stale(self, doc_id: str) -> None:
        """Say that the file behind a compiled page has changed.

        The page is kept - it is what the document said - and the
        reader is told that the file in front of him is not the one it
        was read from (AC-E-015). Nothing is recompiled until the user
        asks for it.
        """
        if not doc_id:
            return
        try:
            await self.sqlite.set_doc_stale(doc_id, True)
        except Exception:  # noqa: BLE001 - the marker is a nicety
            logger.warning("The stale marker of %s was not written", doc_id)

    async def _record_entry_doc_id(
        self, entries: list[dict], file_path: Path, result: dict
    ) -> None:
        """Remember which document a file entry was indexed under."""
        doc_id = result.get("doc_id")
        if not doc_id:
            return
        target = os.path.normcase(str(file_path))
        for entry in entries:
            if entry["kind"] != "file":
                continue
            if os.path.normcase(entry["path"]) != target:
                continue
            setter = getattr(self.sqlite, "set_workspace_entry_doc_id", None)
            if setter is None:
                return
            try:
                await setter(entry["id"], doc_id)
            except Exception:  # noqa: BLE001 - bookkeeping must not fail the ingest
                logger.exception("Cannot record doc id for workspace entry %s", entry["id"])

    async def _report_progress(
        self,
        on_progress: Callable[[int, int, str], None] | None,
        done: int,
        total: int,
        file_path: Path,
    ) -> None:
        if on_progress is None:
            return
        try:
            result = on_progress(done, total, str(file_path))
            # A callback that has to write to the database is async.
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - progress must never break the ingest
            logger.exception("Ingest progress callback failed")

    def _file_size(self, file_path: Path) -> int | None:
        try:
            return file_path.stat().st_size
        except OSError:
            return None

    def _hash_file(self, file_path: Path) -> str:
        """Compute SHA256 hash of file contents."""
        sha = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        return sha.hexdigest()
