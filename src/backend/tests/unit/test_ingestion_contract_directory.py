"""TASK-008 — directory ingestion: ordering, progress and failure.

* AC-F-014 — the progress callback fires once per file with
  (done, total, path), and a callback that raises does not break the
  ingest
* AC-P-002 — files are processed one at a time
* AC-P-004 — the event loop stays responsive while a file is parsed
* AC-E-007 — a file that cannot be read aborts the whole directory
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from yuanwork.ingestion.parser.base import BaseParser, RawBlock
from yuanwork.ingestion.parser.dispatcher import ParserDispatcher
from yuanwork.ingestion.pipeline import IngestionPipeline
from unit.ingestion_fixtures import (
    RecordingSQLite,
    RecordingSemanticMemory,
    make_markdown_file,
    make_text_file,
)


class SlowParser(BaseParser):
    """A parser that records when it is entered and left."""

    SUPPORTED_EXTENSIONS = ("md", "txt")

    def __init__(self, events: list[str] | None = None):
        self.events = events if events is not None else []
        self.active = 0
        self.max_concurrent = 0

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        self.events.append(f"enter:{file_path.name}")
        self.active += 1
        self.max_concurrent = max(self.max_concurrent, self.active)
        # a real parser blocks in a thread; emulate that with a sleep
        await asyncio.sleep(0.01)
        self.active -= 1
        self.events.append(f"leave:{file_path.name}")
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


def make_pipeline(events: list[str], parser: SlowParser | None = None):
    pipeline = IngestionPipeline(
        sqlite_store=RecordingSQLite(events=events),
        semantic_memory=RecordingSemanticMemory(events=events),
    )
    pipeline.dispatcher = ParserDispatcher([parser or SlowParser(events)])
    return pipeline


# --------------------------------------------------------------------------
# AC-F-014 — the progress callback
# --------------------------------------------------------------------------


async def test_progress_callback_fires_once_per_file(temp_dir: Path):
    for name in ("a.md", "b.md", "c.md"):
        make_markdown_file(temp_dir, name)
    events: list[str] = []
    pipeline = make_pipeline(events)

    seen: list[tuple[int, int, str]] = []

    await pipeline.ingest_directory(temp_dir, on_progress=lambda d, t, p: seen.append((d, t, p)))

    assert len(seen) == 3
    assert [d for d, _, _ in seen] == [1, 2, 3]
    assert {t for _, t, _ in seen} == {3}
    assert all(Path(p).exists() for _, _, p in seen)


async def test_progress_callback_is_not_called_for_unsupported_files(temp_dir: Path):
    make_markdown_file(temp_dir, "a.md")
    (temp_dir / "notes.xyz").write_text("hello", encoding="utf-8")
    events: list[str] = []
    pipeline = make_pipeline(events)

    seen: list[tuple[int, int, str]] = []
    await pipeline.ingest_directory(temp_dir, on_progress=lambda d, t, p: seen.append((d, t, p)))

    assert len(seen) == 1
    assert seen[0][1] == 1


async def test_a_failing_progress_callback_does_not_break_the_ingest(temp_dir: Path):
    make_markdown_file(temp_dir, "a.md")
    make_markdown_file(temp_dir, "b.md")
    events: list[str] = []
    pipeline = make_pipeline(events)

    calls: list[int] = []

    def boom(done, total, path):
        calls.append(done)
        raise RuntimeError("callback exploded")

    result = await pipeline.ingest_directory(temp_dir, on_progress=boom)

    assert len(calls) == 2
    assert result["indexed"] == 2


async def test_the_denominator_only_counts_supported_files(temp_dir: Path):
    make_markdown_file(temp_dir, "a.md")
    (temp_dir / "readme.xyz").write_text("x", encoding="utf-8")
    events: list[str] = []
    pipeline = make_pipeline(events)
    seen: list[tuple[int, int, str]] = []
    await pipeline.ingest_directory(temp_dir, on_progress=lambda d, t, p: seen.append((d, t, p)))
    assert seen[0][1] == 1


# --------------------------------------------------------------------------
# AC-P-002 — one file at a time
# --------------------------------------------------------------------------


async def test_files_are_processed_one_at_a_time(temp_dir: Path):
    for name in ("a.md", "b.md", "c.md"):
        make_markdown_file(temp_dir, name)
    events: list[str] = []
    parser = SlowParser(events)
    pipeline = make_pipeline(events, parser)

    await pipeline.ingest_directory(temp_dir)

    assert parser.max_concurrent == 1
    # enter/leave strictly alternate
    parse_events = [e for e in events if e.startswith(("enter", "leave"))]
    assert len(parse_events) == 6
    assert all(parse_events[i].startswith("enter") for i in range(0, 6, 2))
    assert all(parse_events[i].startswith("leave") for i in range(1, 6, 2))


# --------------------------------------------------------------------------
# AC-P-004 — the event loop stays free
# --------------------------------------------------------------------------


async def test_the_event_loop_stays_responsive_during_parsing(temp_dir: Path):
    make_markdown_file(temp_dir, "a.md")
    events: list[str] = []
    pipeline = make_pipeline(events)

    ticks: list[int] = []

    async def heartbeat():
        for _ in range(5):
            ticks.append(1)
            await asyncio.sleep(0.005)

    task = asyncio.create_task(heartbeat())
    await pipeline.ingest_directory(temp_dir)
    await task

    # the heartbeat ran while the pipeline was parsing
    assert len(ticks) == 5


# --------------------------------------------------------------------------
# AC-E-007 — a file that cannot be read
# --------------------------------------------------------------------------


async def test_an_unreadable_file_aborts_the_whole_directory(temp_dir: Path):
    make_markdown_file(temp_dir, "a.md")
    make_markdown_file(temp_dir, "b.md")
    events: list[str] = []
    pipeline = make_pipeline(events)

    real_hash = pipeline._hash_file

    def flaky_hash(path: Path) -> str:
        if path.name == "b.md":
            raise OSError("device not ready")
        return real_hash(path)

    pipeline._hash_file = flaky_hash  # type: ignore[method-assign]

    with pytest.raises(OSError):
        await pipeline.ingest_directory(temp_dir)


async def test_the_batch_continues_past_a_parser_failure(temp_dir: Path):
    """A *parse* failure is caught and counted; only an IO failure on the
    hash escapes."""
    make_markdown_file(temp_dir, "a.md")
    make_markdown_file(temp_dir, "b.md")
    events: list[str] = []
    pipeline = make_pipeline(events)

    original = pipeline.dispatcher.get_parser

    def pick(path: Path):
        parser = original(path)

        async def maybe_boom(file_path, doc_id):
            if file_path.name == "a.md":
                raise ValueError("cannot parse")
            return await SlowParser.parse(parser, file_path, doc_id)

        parser.parse = maybe_boom  # type: ignore[method-assign]
        return parser

    pipeline.dispatcher.get_parser = pick  # type: ignore[method-assign]

    result = await pipeline.ingest_directory(temp_dir)

    assert result["errors"] == 1
    assert result["indexed"] == 1


# --------------------------------------------------------------------------
# the results summary
# --------------------------------------------------------------------------


async def test_the_summary_counts_each_outcome(temp_dir: Path):
    make_markdown_file(temp_dir, "a.md")
    make_markdown_file(temp_dir, "b.md")
    events: list[str] = []
    store = RecordingSQLite(events=events)
    pipeline = IngestionPipeline(
        sqlite_store=store, semantic_memory=RecordingSemanticMemory(events=events)
    )
    pipeline.dispatcher = ParserDispatcher([SlowParser(events)])

    # 'a.md' is already known -> skipped
    store.existing = {"doc_id": "known"}
    result = await pipeline.ingest_directory(temp_dir)
    assert result["skipped"] == 2
    assert result["indexed"] == 0
    assert len(result["files"]) == 2
    assert all("path" in entry for entry in result["files"])


async def test_a_missing_directory_is_reported_as_an_error(temp_dir: Path):
    events: list[str] = []
    pipeline = make_pipeline(events)
    result = await pipeline.ingest_directory(temp_dir / "nope")
    assert result == {"status": "error", "error": "Directory not found"}
