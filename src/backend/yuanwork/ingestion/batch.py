"""A build that reads in parallel and writes in batches.

A build of thousands of files spends its time in two places: reading
the documents, and turning the slices into vectors. Both are worth
doing in bigger pieces than one file at a time.

* the **reading** happens in a few worker processes. Parsing is CPU
  bound and releases the GIL in the libraries that do the work, so
  three or four processes really do parse three or four times as
  fast (AC-P-001).
* the **writing** happens in batches - one embedding call for a batch
  of slices instead of one per file, at a capped rate so a build does
  not starve the rest of the machine (AC-P-012).

Two rules the build keeps:

* **it can be resumed.** A file that is already indexed is skipped; a
  run that was interrupted halfway picks up where it stopped. A file
  that changed on disk is not silently replaced either - the old
  version stays and is marked (AC-E-015).
* **it is finished when the long tail is searchable.** The core
  layer's pages are compiled in the background - the pages of a
  contract are worth having, but they are not worth holding up the
  build for (AC-F-026).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: How many files are parsed at the same time when nothing says
#: otherwise. This used to be a flat 4 — back then the embedding was
#: the slow part, and four readers were plenty. With the GPU doing the
#: embedding the parsing is what needs the cores, and the readers were
#: left sitting idle waiting for the writer (AC-P-010).
DEFAULT_WORKERS = 4

#: How many of the machine's physical cores the readers may have when
#: nothing is configured. Half: the writing side wants the CPU too
#: (jieba cuts the words, tantivy writes the index), and a build that
#: takes every core leaves the machine unusable.
WORKER_CORE_SHARE = 0.5


def default_workers() -> int:
    """How many files to parse at the same time, from the machine.

    Half of the physical cores. The logical count is not the right
    number: the extra threads are the hyper-threaded halves of the same
    cores, and two readers on one core do not do twice the work.
    """
    try:
        from yuanwork.config import get_settings

        settings = get_settings()
        configured = int(getattr(settings, "ingest_workers", 0) or 0)
        if configured > 0:
            return configured
    except Exception:  # noqa: BLE001 - the default is enough
        pass

    physical = _physical_cores()
    if physical <= 1:
        return 1
    return max(1, int(physical * WORKER_CORE_SHARE))


def _physical_cores() -> int:
    """How many physical cores the machine has."""
    try:
        import psutil

        count = psutil.cpu_count(logical=False)
        if count:
            return int(count)
    except Exception:  # noqa: BLE001 - the logical count is the fallback
        pass
    import os

    return max(1, os.cpu_count() or 1)

#: How many parsed files wait for the writer at most. More than this
#: and the memory goes on building slices nobody is going to read yet.
DEFAULT_MAX_IN_FLIGHT = 16

#: How many core-layer compilations may wait for the model at the same
#: time. Each waiting call holds a thread, and the embedding needs
#: threads too - a build that starts one compilation per file takes
#: the pool away from the writing.
MAX_CONCURRENT_COMPILES = 2

#: The compilations that are still running. A module-level set keeps a
#: strong reference to them - a task nobody holds on to can disappear
#: halfway through its work.
BACKGROUND: set[asyncio.Task] = set()


#: The parsers of this process, built once.
#:
#: Building them is not free: the image parser loads the OCR engine and
#: its models - **measured at 21 seconds** - and a worker that builds
#: them again for every file pays that again and again. The real
#: workspace has 105 images, so that was 2200 seconds of the build's
#: parsing time. One set per worker process is the fix.
_PARSERS: dict[str, Any] = {}


def _dispatcher() -> Any:
    """The parser dispatcher of this process - built once."""
    dispatcher = _PARSERS.get("dispatcher")
    if dispatcher is None:
        from yuanwork.ingestion.parser.dispatcher import ParserDispatcher

        dispatcher = ParserDispatcher()
        _PARSERS["dispatcher"] = dispatcher
    return dispatcher


def parse_one(job: tuple[str, str, str]) -> tuple[str, str, str, list[Any], str | None, float]:
    """Parse one file.

    This runs in a worker process, so it has to be a plain function
    that can be pickled - not a method, not a closure.

    The job comes back with the answer: which file this is. The build
    reads many files at once, and a result that does not say which
    file it belongs to would be filed under the wrong one.
    """
    path, doc_id, file_hash = job

    # One thread per worker. The OCR engine's ONNX session opens as
    # many threads as the machine has cores, and seven workers doing
    # that is 98 threads on 14 cores - they spin, they fight, and the
    # whole build slows down. This has to be set before onnxruntime is
    # imported, and the engine is the one that imports it.
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MKL_NUM_THREADS", "1")

    parser = _dispatcher().get_parser(Path(path))
    if parser is None:
        return path, doc_id, file_hash, [], "没有可用的解析器", 0.0

    started = time.perf_counter()
    try:
        blocks = asyncio.run(parser.parse(Path(path), doc_id))
    except Exception as e:  # noqa: BLE001 - one file is not the whole build
        return path, doc_id, file_hash, [], repr(e), time.perf_counter() - started
    return path, doc_id, file_hash, blocks, None, time.perf_counter() - started


def doc_id_for(file_path: Path, file_hash: str) -> str:
    """The identifier of a document - the same one the pipeline uses."""
    return hashlib.sha256(f"{file_path}|{file_hash}".encode()).hexdigest()[:16]


class BatchIngestor:
    """Indexes a workspace: in parallel, in batches, resumable."""

    def __init__(
        self,
        pipeline: Any,
        workers: int | None = None,
        max_in_flight: int | None = None,
        executor: Any | None = None,
    ):
        self.pipeline = pipeline
        self.workers = workers or default_workers()
        self.max_in_flight = max_in_flight or DEFAULT_MAX_IN_FLIGHT
        #: The worker pool. A test can hand in its own.
        self.executor = executor
        #: The pool the last build used.
        self.pool: Any | None = None
        #: The core layer's compilations that are still running.
        self.background: list[asyncio.Task] = []
        #: How many core-layer compilations may wait for the model at the
        #: same time. Each waiting call holds a thread. It is made when
        #: the build starts - a semaphore belongs to the loop it is
        #: used in.
        self._semaphore: asyncio.Semaphore | None = None

    # --------------------------------------------------------------------------
    # the build
    # --------------------------------------------------------------------------

    async def ingest(
        self,
        root: Path | None = None,
        recursive: bool = True,
        force: bool = False,
        on_progress: Callable[[int, int, str], None] | None = None,
        files: list[Path] | None = None,
    ) -> dict[str, Any]:
        """Index everything under ``root``, or exactly the files given.

        The build is finished when the long tail is searchable. The
        core layer's pages are compiled in the background and do not
        hold it up (AC-F-026).

        ``files`` is the list of files to index - the workspace build
        hands the files the workspace covers over (TASK-051). Without
        it the build reads the supported files under ``root``, exactly
        as it always did.
        """
        if files is None:
            if root is None:
                raise ValueError("A build needs a folder or a list of files")
            files = self.pipeline.supported_files(Path(root), recursive)
        else:
            files = [Path(f) for f in files]

        results: dict[str, Any] = {
            "status": "completed",
            "indexed": 0,
            "skipped": 0,
            "errors": 0,
            "files": [],
            "compiled_in_background": 0,
            "core_pending": 0,
        }

        jobs: list[tuple[str, str, str]] = []
        skipped_files = 0
        for path in files:
            file_hash = self.pipeline.hash_file(path)
            doc_id = doc_id_for(path, file_hash)
            if not force:
                # The resume: what the index already knows about this
                # file decides whether it is read at all.
                state = await self.pipeline.resume_state(path, file_hash)
                if state["state"] == "already_indexed":
                    results["skipped"] += 1
                    skipped_files += 1
                    results["files"].append(
                        {
                            "path": str(path),
                            "status": "skipped",
                            "reason": "already_indexed",
                            "doc_id": state.get("doc_id"),
                        }
                    )
                    continue
                if state["state"] == "modified":
                    # The file changed on disk. The indexed version is
                    # kept and marked - it is not replaced without
                    # being asked (AC-E-015).
                    results["skipped"] += 1
                    skipped_files += 1
                    results["files"].append(
                        {
                            "path": str(path),
                            "status": "skipped",
                            "reason": "modified",
                            "doc_id": state.get("doc_id"),
                            "stale": True,
                        }
                    )
                    continue
            else:
                # An explicit rebuild replaces the indexed version of
                # this file - otherwise the old one would stay
                # searchable next to the new one.
                await self.pipeline.drop_previous_version(path, doc_id)
            jobs.append((str(path), doc_id, file_hash))

        # The denominator is every file the build looked at - the ones
        # that were already in the index included. They *are* in the
        # index, that is the whole point of the resume, so counting
        # them as done is what keeps the progress honest: a build over
        # a workspace whose files are already indexed ends at 100%,
        # not at 20%.
        total = len(files)

        if not jobs:
            # Everything was already in the index - the build is done.
            if total:
                await self._report(on_progress, total, total, str(files[0]))
            return results

        if skipped_files:
            await self._report(on_progress, skipped_files, total, str(files[0]))

        loop = asyncio.get_running_loop()
        pool = self.executor
        owned = pool is None
        if owned:
            pool = ProcessPoolExecutor(max_workers=self.workers)
        self.pool = pool

        # How many workers this build reads with. The pipeline keeps it
        # so that the numbers can say what the build actually delivers:
        # a parallel build parses several files at the same time, and a
        # rate per core is not what the user waits for.
        try:
            self.pipeline.build_workers = self.workers
        except Exception:  # noqa: BLE001 - a stand-in pipeline may not allow it
            pass

        # A pool of processes reads the files in other processes; a pool
        # of threads reads them here, with the pipeline's own parsers.
        if isinstance(pool, ProcessPoolExecutor):
            def submit(job: tuple[str, str, str]):
                return loop.run_in_executor(pool, parse_one, job)
        else:
            def submit(job: tuple[str, str, str]):
                return asyncio.ensure_future(self._parse_here(job))

        # The slices go in batches - one embedding call per batch
        # instead of one per file (AC-P-012).
        batch = self._make_batch()
        previous_batch = getattr(self.pipeline, "_batch", None)
        self.pipeline._batch = batch

        pending: set[asyncio.Future] = set()
        # The files that were already in the index count as done.
        done_files = skipped_files
        try:
            for path, doc_id, file_hash in jobs:
                pending.add(submit((path, doc_id, file_hash)))
                # The cap: more than this many parsed files waiting
                # for the writer, and the memory goes on building
                # slices nobody is going to read yet.
                while len(pending) >= self.max_in_flight:
                    done, pending = await asyncio.wait(
                        pending, return_when=asyncio.FIRST_COMPLETED
                    )
                    for future in done:
                        done_files += 1
                        await self._handle(future, results)
                        await self._report(on_progress, done_files, total, path)

            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for future in done:
                    done_files += 1
                    await self._handle(future, results)
                    await self._report(on_progress, done_files, total, path)
        finally:
            if owned:
                pool.shutdown(wait=True)

        # Whatever the writer still holds goes in - the long tail has to
        # be searchable when the build says it is done.
        await self.pipeline.flush_batch()
        self.pipeline._batch = previous_batch

        # The full-text index is written in the background - it does not
        # hold up the build - but the long tail has to be searchable
        # when the build says it is done (AC-F-026). So the build ends
        # with one flush.
        flush_fts = getattr(self.pipeline.semantic, "flush_fts", None)
        if flush_fts is not None:
            try:
                await flush_fts()
            except Exception as e:  # noqa: BLE001 - a failed flush is not a failed build
                logger.warning(f"The full-text index was not flushed: {e!r}")

        self._record_timings(batch)
        results["compiled_in_background"] = len(self.background)
        results["core_pending"] = len(self.background)
        return results

    # --------------------------------------------------------------------------
    # one file
    # --------------------------------------------------------------------------

    async def _parse_here(
        self, job: tuple[str, str, str]
    ) -> tuple[str, str, str, list[Any], str | None, float]:
        """Parse one file in this process.

        Used when the caller brings its own pool: the pipeline's own
        parsers are then the ones that read the files.
        """
        path, doc_id, file_hash = job
        parser = self.pipeline.dispatcher.get_parser(Path(path))
        if parser is None:
            return path, doc_id, file_hash, [], "没有可用的解析器", 0.0

        started = time.perf_counter()
        try:
            blocks = await parser.parse(Path(path), doc_id)
        except Exception as e:  # noqa: BLE001 - one file is not the whole build
            return path, doc_id, file_hash, [], repr(e), time.perf_counter() - started
        return path, doc_id, file_hash, blocks, None, time.perf_counter() - started

    async def _handle(
        self,
        future: asyncio.Future,
        results: dict[str, Any],
    ) -> None:
        """What one parsed file amounts to.

        Which file this is comes out of the answer, not out of the loop
        that handed the work out - several files are read at the same
        time.
        """
        try:
            path, parsed_doc_id, file_hash, blocks, error, seconds = future.result()
        except Exception as e:  # noqa: BLE001 - one file is not the whole build
            results["errors"] += 1
            results["files"].append(
                {"path": path, "status": "error", "error": repr(e)}
            )
            return

        self.pipeline._record("parse", seconds)

        if error:
            results["errors"] += 1
            results["files"].append(
                {"path": path, "status": "error", "error": error}
            )
            return

        file_path = Path(path)
        try:
            indexed = await self.pipeline.index_blocks(
                file_path, parsed_doc_id, file_hash, blocks, compile_now=False
            )
        except Exception as e:  # noqa: BLE001 - one file is not the whole build
            results["errors"] += 1
            results["files"].append(
                {"path": path, "status": "error", "error": repr(e)}
            )
            return

        if indexed.get("status") != "indexed":
            results["skipped"] += 1
            results["files"].append({"path": path, **indexed})
            return

        # The core layer's pages are compiled in the background: the
        # build is finished when the long tail is searchable, not when
        # the last contract has been read (AC-F-026).
        if str(indexed.get("tier")) == "core":
            task = asyncio.create_task(self._compile_in_background(
                parsed_doc_id,
                file_path,
                blocks,
                {"tier": indexed.get("tier")},
                file_hash,
            ))
            self.background.append(task)
            BACKGROUND.add(task)
            task.add_done_callback(BACKGROUND.discard)

        results["indexed"] += 1
        results["files"].append({"path": path, **indexed})

    # --------------------------------------------------------------------------
    # the background
    # --------------------------------------------------------------------------

    async def _compile_in_background(
        self,
        doc_id: str,
        file_path: Path,
        blocks: list,
        tier: dict,
        file_hash: str,
    ) -> None:
        """Compile a core document, a few at a time.

        Every compilation waits for the model, and every waiting call
        holds a thread while it waits. One compilation per file would
        take the thread pool away from the embedding and the writing -
        and the build would crawl exactly when the model is slow or
        unreachable, which is when the user can least afford it.
        """
        async with self._compile_slots():
            await self.pipeline.compile_document(
                doc_id, file_path, blocks, tier, file_hash
            )

    def _compile_slots(self) -> asyncio.Semaphore:
        """The slots the compilations take, made once per build."""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_COMPILES)
        return self._semaphore

    async def wait_for_background(self) -> None:
        """Wait for the core layer's pages."""
        if not self.background:
            return
        await asyncio.gather(*self.background, return_exceptions=True)
        self.background = []

    # --------------------------------------------------------------------------
    # the pieces
    # --------------------------------------------------------------------------

    def _make_batch(self):
        """The writer the build uses."""
        from yuanwork.ingestion.pipeline import (
            DEFAULT_INDEX_BATCH_SIZE,
            DEFAULT_INDEX_RATE_LIMIT,
            ChunkBatch,
        )

        return ChunkBatch(
            self.pipeline.semantic,
            batch_size=getattr(
                self.pipeline, "index_batch_size", DEFAULT_INDEX_BATCH_SIZE
            ),
            rate_limit=getattr(
                self.pipeline, "index_rate_limit", DEFAULT_INDEX_RATE_LIMIT
            ),
        )

    def _record_timings(self, batch) -> None:
        """Where the time of the build went (AC-P-010/011/012)."""
        timings = getattr(batch, "timings", None) or {}
        self.pipeline._record("embed", timings.get("embed", 0.0))
        self.pipeline._record("write", timings.get("write", 0.0))

    async def _report(
        self,
        on_progress: Callable[[int, int, str], None] | None,
        done: int,
        total: int,
        path: str,
    ) -> None:
        """Tell the caller how far the build is."""
        if on_progress is None:
            return
        try:
            result = on_progress(done, total, path)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - progress must never break the build
            logger.exception("The progress callback failed")
