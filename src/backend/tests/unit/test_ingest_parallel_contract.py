"""TASK-045 — the bulk build reads in parallel and writes in batches.

A build of thousands of files is only worth doing in bigger pieces:
the files are read in worker processes, the slices are written in
batches, and a run that was interrupted picks up where it stopped.

* AC-P-001 — the build reaches a searchable state: the files are read
  in parallel, at most so many at a time, and the slices are written
  in batches
* AC-P-012 — the write stage goes in batches, and the build says how
  long each stage took
* AC-F-026 — the build is finished when the long tail is searchable;
  the core layer's pages are compiled in the background and do not
  hold the build up
"""

from __future__ import annotations

import asyncio
import pickle
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.ingestion.batch import BatchIngestor, doc_id_for, parse_one
from yuanwork.ingestion.parser.base import BaseParser, RawBlock
from yuanwork.ingestion.parser.dispatcher import ParserDispatcher
from yuanwork.ingestion.pipeline import IngestionPipeline


# --------------------------------------------------------------------------
# the pieces
# --------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class RecordingSemantic:
    """The writes, as they happen."""

    def __init__(self):
        self.batches: list[list[str]] = []
        self.last_timings = {"embed": 0.0, "write": 0.0}

    async def add_chunks(self, chunks):
        self.batches.append([c["id"] for c in chunks])
        return [c["id"] for c in chunks]

    async def delete_by_doc_id(self, doc_id):
        return None

    @property
    def written(self) -> int:
        return sum(len(batch) for batch in self.batches)


class SlowParser(BaseParser):
    """A parser that says when it is busy."""

    SUPPORTED_EXTENSIONS = ("md",)

    def __init__(self):
        self.active = 0
        self.max_concurrent = 0
        self.parsed: list[str] = []

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        self.active += 1
        self.max_concurrent = max(self.max_concurrent, self.active)
        await asyncio.sleep(0.05)
        self.active -= 1
        self.parsed.append(file_path.name)
        return [
            RawBlock(
                block_id=f"{doc_id}_b1",
                doc_id=doc_id,
                content="内容",
                type="text",
                meta={},
            )
        ]

    async def extract_metadata(self, file_path: Path) -> dict:
        return {}


def make_pipeline(store, semantic, parser: BaseParser | None = None):
    """A pipeline with the real store and a recording writer."""
    pipeline = IngestionPipeline(sqlite_store=store, semantic_memory=semantic)
    if parser is not None:
        pipeline.dispatcher = ParserDispatcher([parser])
    return pipeline


def write_files(folder: Path, count: int, name: str = "文件") -> list[Path]:
    """Some markdown files to read."""
    written = []
    for index in range(count):
        path = folder / f"{name}{index}.md"
        path.write_text(
            f"# {name}{index}\n\n" + ("内容" * 200) + "\n", encoding="utf-8"
        )
        written.append(path)
    return written


# --------------------------------------------------------------------------
# the reading
# --------------------------------------------------------------------------


def test_the_worker_function_can_be_pickled():
    """A worker process can only be handed a plain function."""
    assert parse_one.__module__ == "yuanwork.ingestion.batch"
    assert pickle.loads(pickle.dumps(parse_one)) is parse_one


async def test_the_build_reads_the_files_in_worker_processes(
    store, tmp_path: Path
):
    """The files are read in other processes - parsing is CPU bound."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    write_files(tmp_path, 3)

    ingestor = BatchIngestor(pipeline, workers=2)
    result = await ingestor.ingest(tmp_path)

    assert result["indexed"] == 3
    assert result["errors"] == 0
    assert semantic.written > 0
    assert isinstance(ingestor.pool, ProcessPoolExecutor)


async def test_the_files_are_read_at_the_same_time(store, tmp_path: Path):
    """Reading is parallel - not one file after the other."""
    semantic = RecordingSemantic()
    parser = SlowParser()
    pipeline = make_pipeline(store, semantic, parser)
    write_files(tmp_path, 4)

    ingestor = BatchIngestor(
        pipeline, max_in_flight=4, executor=ThreadPoolExecutor(4)
    )
    result = await ingestor.ingest(tmp_path)

    assert result["indexed"] == 4
    assert parser.max_concurrent > 1, "the files were read one at a time"


async def test_at_most_so_many_files_are_in_flight(store, tmp_path: Path):
    """More files in flight than that, and the memory goes on building
    slices nobody is going to read yet."""
    semantic = RecordingSemantic()
    parser = SlowParser()
    pipeline = make_pipeline(store, semantic, parser)
    write_files(tmp_path, 6)

    ingestor = BatchIngestor(
        pipeline, max_in_flight=2, executor=ThreadPoolExecutor(6)
    )
    result = await ingestor.ingest(tmp_path)

    assert result["indexed"] == 6
    assert parser.max_concurrent <= 2


async def test_the_number_of_workers_follows_the_machine():
    """A build does not use more workers than the machine has cores."""
    import os

    ingestor = BatchIngestor(pipeline=None)
    assert 1 <= ingestor.workers <= max(1, os.cpu_count() or 1)


async def test_the_workers_are_half_of_the_physical_cores():
    """The readers get half the physical cores, not all of them.

    The writing side wants the CPU too — jieba cuts the words and
    tantivy writes the index — and a build that takes every core
    leaves the machine unusable while it runs.
    """
    from yuanwork.ingestion.batch import _physical_cores

    ingestor = BatchIngestor(pipeline=None)
    physical = _physical_cores()
    assert ingestor.workers == max(1, int(physical * 0.5))


async def test_the_number_of_workers_can_be_configured(monkeypatch):
    """The machine decides by default; the user can say otherwise."""
    from yuanwork.config import YuanWorkSettings
    import yuanwork.config as config_module

    settings = YuanWorkSettings(data_dir=Path("."), ingest_workers=3)
    monkeypatch.setattr(config_module, "_settings", settings)
    try:
        ingestor = BatchIngestor(pipeline=None)
        assert ingestor.workers == 3
    finally:
        config_module.reset_settings()


async def test_a_number_of_workers_given_to_the_ingestor_wins():
    """What the caller asks for is what it gets."""
    ingestor = BatchIngestor(pipeline=None, workers=7)
    assert ingestor.workers == 7


# --------------------------------------------------------------------------
# the writing
# --------------------------------------------------------------------------


async def test_the_slices_are_written_in_batches(store, tmp_path: Path):
    """One embedding call per batch, not one per file."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    pipeline.index_batch_size = 100
    write_files(tmp_path, 5)

    ingestor = BatchIngestor(pipeline, executor=ThreadPoolExecutor(2))
    result = await ingestor.ingest(tmp_path)

    assert result["indexed"] == 5
    assert len(semantic.batches) == 1, "the build wrote one file at a time"


async def test_the_build_says_where_the_time_went(store, tmp_path: Path):
    """The stages are measured - that is what the benchmark reads
    (AC-P-010/011/012)."""
    semantic = RecordingSemantic()
    semantic.last_timings = {"embed": 1.5, "write": 0.5}
    pipeline = make_pipeline(store, semantic)
    write_files(tmp_path, 2)

    ingestor = BatchIngestor(pipeline, executor=ThreadPoolExecutor(2))
    await ingestor.ingest(tmp_path)

    assert pipeline.timings["parse"] > 0
    assert pipeline.timings["embed"] == pytest.approx(1.5)
    assert pipeline.timings["write"] == pytest.approx(0.5)


# --------------------------------------------------------------------------
# the resume
# --------------------------------------------------------------------------


async def test_a_file_that_is_already_indexed_is_skipped(store, tmp_path: Path):
    """A build that was interrupted picks up where it stopped."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    write_files(tmp_path, 2)

    first = BatchIngestor(pipeline, executor=ThreadPoolExecutor(2))
    await first.ingest(tmp_path)

    second = BatchIngestor(pipeline, executor=ThreadPoolExecutor(2))
    result = await second.ingest(tmp_path)

    assert result["indexed"] == 0
    assert result["skipped"] == 2
    assert all(f["reason"] == "already_indexed" for f in result["files"])


async def test_a_changed_file_is_kept_and_marked(store, tmp_path: Path):
    """The old version stays in the index - it is not replaced without
    being asked (AC-E-015)."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    path = write_files(tmp_path, 1)[0]

    first = BatchIngestor(pipeline, executor=ThreadPoolExecutor(1))
    await first.ingest(tmp_path)
    doc_id = doc_id_for(path, pipeline.hash_file(path))

    # The file changes on disk.
    path.write_text("# 改过了\n\n新的内容\n", encoding="utf-8")
    assert doc_id_for(path, pipeline.hash_file(path)) != doc_id

    second = BatchIngestor(pipeline, executor=ThreadPoolExecutor(1))
    result = await second.ingest(tmp_path)

    assert result["indexed"] == 0
    assert result["skipped"] == 1
    assert result["files"][0]["reason"] == "modified"
    # The old version is marked - the reader has to know.
    assert doc_id in await store.list_stale_doc_ids()


async def test_an_explicit_rebuild_replaces_the_old_version(
    store, tmp_path: Path
):
    """The user asked for it this time."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    path = write_files(tmp_path, 1)[0]

    first = BatchIngestor(pipeline, executor=ThreadPoolExecutor(1))
    await first.ingest(tmp_path)
    old_doc_id = doc_id_for(path, pipeline.hash_file(path))

    path.write_text("# 改过了\n\n新的内容\n", encoding="utf-8")

    second = BatchIngestor(pipeline, executor=ThreadPoolExecutor(1))
    result = await second.ingest(tmp_path, force=True)

    assert result["indexed"] == 1
    # The old version is gone from the index.
    assert await store.get_file(old_doc_id) is None


# --------------------------------------------------------------------------
# when is the build finished
# --------------------------------------------------------------------------


async def test_the_build_is_finished_when_the_long_tail_is_searchable(
    store, tmp_path: Path
):
    """The slices are written when the build says it is done - the core
    layer's pages are not waited for (AC-F-026)."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    write_files(tmp_path, 2)
    # A document whose path says it belongs to the core layer.
    (tmp_path / "采购合同.md").write_text(
        "# 采购合同\n\n" + ("条款" * 500) + "\n", encoding="utf-8"
    )

    compiled: list[str] = []

    async def slow_compile(doc_id, file_path, blocks, tier, file_hash=""):
        await asyncio.sleep(0.2)
        compiled.append(doc_id)
        return {"compiled": True, "pages": [{"page_id": "p-1"}]}

    pipeline.compile_document = slow_compile

    ingestor = BatchIngestor(pipeline, executor=ThreadPoolExecutor(2))
    result = await ingestor.ingest(tmp_path)

    # The long tail is written ...
    assert semantic.written > 0
    assert result["status"] == "completed"
    # ... and the pages of the core layer are still being compiled.
    assert result["compiled_in_background"] == 1
    assert compiled == []


async def test_the_core_pages_are_compiled_in_the_background(
    store, tmp_path: Path
):
    """The compilation happens - just not in the way of the build."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    # A file whose path says it is a core document.
    core = tmp_path / "采购合同.md"
    core.write_text("# 采购合同\n\n" + ("条款" * 500) + "\n", encoding="utf-8")

    compiled: list[str] = []

    async def slow_compile(doc_id, file_path, blocks, tier, file_hash=""):
        await asyncio.sleep(0.05)
        compiled.append(doc_id)
        return {"compiled": True, "pages": [{"page_id": "p-1"}]}

    pipeline.compile_document = slow_compile

    ingestor = BatchIngestor(pipeline, executor=ThreadPoolExecutor(1))
    result = await ingestor.ingest(tmp_path)

    assert result["compiled_in_background"] == 1
    assert compiled == []

    await ingestor.wait_for_background()

    assert len(compiled) == 1


async def test_a_long_tail_document_is_not_compiled(store, tmp_path: Path):
    """Only the core layer's documents get pages."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    write_files(tmp_path, 1, name="随手记")

    compiled: list[str] = []

    async def never(doc_id, file_path, blocks, tier, file_hash=""):
        compiled.append(doc_id)
        return {"compiled": True, "pages": []}

    pipeline.compile_document = never

    ingestor = BatchIngestor(pipeline, executor=ThreadPoolExecutor(1))
    result = await ingestor.ingest(tmp_path)

    assert result["indexed"] == 1
    assert result["compiled_in_background"] == 0
    await ingestor.wait_for_background()
    assert compiled == []


# --------------------------------------------------------------------------
# when something goes wrong
# --------------------------------------------------------------------------


async def test_a_file_that_cannot_be_read_does_not_stop_the_build(
    store, tmp_path: Path
):
    """One broken file is not the whole build (AC-E-005)."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    write_files(tmp_path, 2)
    # A file with the extension of a PDF and the bytes of nothing.
    (tmp_path / "坏文件.pdf").write_bytes(b"not a pdf at all")

    ingestor = BatchIngestor(pipeline, executor=ThreadPoolExecutor(2))
    result = await ingestor.ingest(tmp_path)

    assert result["indexed"] == 2
    assert result["errors"] == 1
    broken = [f for f in result["files"] if f["status"] == "error"]
    assert len(broken) == 1
    assert broken[0]["error"]


async def test_a_failing_progress_callback_does_not_break_the_build(
    store, tmp_path: Path
):
    """Progress is a nicety - it must never break a build."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    write_files(tmp_path, 2)

    calls: list[int] = []

    def boom(done, total, path):
        calls.append(done)
        raise RuntimeError("callback exploded")

    ingestor = BatchIngestor(pipeline, executor=ThreadPoolExecutor(2))
    result = await ingestor.ingest(tmp_path, on_progress=boom)

    assert result["indexed"] == 2
    assert len(calls) == 2


async def test_the_progress_is_reported(store, tmp_path: Path):
    """The caller sees how far the build is."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    write_files(tmp_path, 3)

    seen: list[tuple[int, int, str]] = []

    async def progress(done, total, path):
        seen.append((done, total, path))

    ingestor = BatchIngestor(pipeline, executor=ThreadPoolExecutor(3))
    await ingestor.ingest(tmp_path, on_progress=progress)

    assert len(seen) == 3
    assert {total for _, total, _ in seen} == {3}
    assert all(Path(path).exists() for _, _, path in seen)


# --------------------------------------------------------------------------
# what the build leaves behind
# --------------------------------------------------------------------------


async def test_the_files_are_written_into_the_index(store, tmp_path: Path):
    """The build's output is in the store, not just in the report."""
    semantic = RecordingSemantic()
    pipeline = make_pipeline(store, semantic)
    write_files(tmp_path, 2)

    ingestor = BatchIngestor(pipeline, executor=ThreadPoolExecutor(2))
    await ingestor.ingest(tmp_path)

    files = await store.list_files()
    assert len(files) == 2
    assert all(f["chunk_count"] > 0 for f in files)
