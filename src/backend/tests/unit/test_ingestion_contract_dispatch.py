"""TASK-002 — extension dispatch and pipeline step order.

Pins two contracts:

* which extension goes to which parser, and that the matching ignores
  case (AC-F-001, AC-F-002);
* the order in which the ingestion pipeline walks its steps
  (AC-F-003): parse → chunk → extract metadata → embed/index → record.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from yuanwork.ingestion.parser.base import BaseParser
from yuanwork.ingestion.parser.dispatcher import ParserDispatcher
from yuanwork.ingestion.pipeline import IngestionPipeline
from unit.ingestion_fixtures import (
    RecordingChunker,
    RecordingParser,
    RecordingSQLite,
    RecordingSemanticMemory,
    make_markdown_file,
)


# --------------------------------------------------------------------------
# AC-F-001 / AC-F-002 — extension dispatch
# --------------------------------------------------------------------------

EXPECTED_DISPATCH = {
    "pdf": "PDFParser",
    "docx": "DocxParser",
    "doc": "DocxParser",
    "pptx": "PptxParser",
    "ppt": "PptxParser",
    "xlsx": "XlsxParser",
    "xls": "XlsxParser",
    "md": "MarkdownTxtParser",
    "markdown": "MarkdownTxtParser",
    "txt": "MarkdownTxtParser",
    "png": "ImageParser",
    "jpg": "ImageParser",
    "jpeg": "ImageParser",
    "bmp": "ImageParser",
    "tiff": "ImageParser",
    "tif": "ImageParser",
    "webp": "ImageParser",
}


@pytest.mark.parametrize("ext,parser_name", sorted(EXPECTED_DISPATCH.items()))
def test_extension_maps_to_expected_parser(ext: str, parser_name: str):
    dispatcher = ParserDispatcher()
    parser = dispatcher.get_parser(Path(f"sample.{ext}"))
    assert parser is not None, f"no parser for .{ext}"
    assert type(parser).__name__ == parser_name


def test_supported_extension_set_is_exactly_17():
    dispatcher = ParserDispatcher()
    exts = dispatcher.supported_extensions()
    assert len(exts) == 17
    assert exts == set(EXPECTED_DISPATCH)


@pytest.mark.parametrize("ext", sorted(EXPECTED_DISPATCH))
def test_dispatch_ignores_extension_case(ext: str):
    dispatcher = ParserDispatcher()
    lower = dispatcher.get_parser(Path(f"sample.{ext}"))
    upper = dispatcher.get_parser(Path(f"sample.{ext.upper()}"))
    mixed = dispatcher.get_parser(Path(f"sample.{ext.capitalize()}"))
    assert type(lower) is type(upper) is type(mixed)


@pytest.mark.parametrize("name", ["sample.xyz", "sample", "sample.tar.gz", ".hidden"])
def test_unsupported_names_return_no_parser(name: str):
    assert ParserDispatcher().get_parser(Path(name)) is None


def test_first_matching_parser_wins():
    """The dispatcher returns the *first* parser that claims the file."""
    calls = []

    class Greedy(BaseParser):
        SUPPORTED_EXTENSIONS = ("md",)

        def __init__(self, name: str):
            self.name = name

        async def parse(self, file_path: Path, doc_id: str):
            return []

        async def extract_metadata(self, file_path: Path):
            return {}

        def can_parse(self, file_path: Path) -> bool:
            calls.append(self.name)
            return file_path.suffix.lower().lstrip(".") in self.SUPPORTED_EXTENSIONS

    dispatcher = ParserDispatcher([Greedy("first"), Greedy("second")])
    parser = dispatcher.get_parser(Path("a.md"))
    assert parser.name == "first"
    assert calls == ["first"]


# --------------------------------------------------------------------------
# AC-F-003 — pipeline step order
# --------------------------------------------------------------------------


async def test_pipeline_steps_run_in_order(temp_dir: Path):
    events: list[str] = []
    path = make_markdown_file(temp_dir, "doc.md", "# 标题\n\n正文\n")

    pipeline = IngestionPipeline(
        sqlite_store=RecordingSQLite(events=events), semantic_memory=RecordingSemanticMemory(events=events)
    )
    parser = RecordingParser(events)
    pipeline.dispatcher = ParserDispatcher([parser])
    pipeline.chunker = RecordingChunker(events)

    result = await pipeline.ingest_file(path)

    assert result["status"] == "indexed"
    assert events == ["hash_lookup", "parse", "chunk", "metadata", "embed", "upsert"]


async def test_pipeline_injects_file_dimensions_into_chunks(temp_dir: Path):
    events: list[str] = []
    path = make_markdown_file(temp_dir, "doc.md", "# 标题\n\n正文\n")

    pipeline = IngestionPipeline(
        sqlite_store=RecordingSQLite(events=events), semantic_memory=RecordingSemanticMemory(events=events)
    )
    pipeline.dispatcher = ParserDispatcher([RecordingParser(events)])
    pipeline.chunker = RecordingChunker(events)

    await pipeline.ingest_file(path)

    chunk = pipeline.semantic.added[0]
    assert chunk["file_name"] == "doc.md"
    assert chunk["file_path"] == str(path)
    assert chunk["file_type"] == "md"
    assert chunk["meta"]["file_meta"] == {"size": 1}


async def test_pipeline_reports_missing_file_without_raising(temp_dir: Path):
    events: list[str] = []
    pipeline = IngestionPipeline(
        sqlite_store=RecordingSQLite(events=events), semantic_memory=RecordingSemanticMemory(events=events)
    )
    result = await pipeline.ingest_file(temp_dir / "nope.md")
    assert result["status"] == "error"
    assert result["error"] == "File not found"


async def test_pipeline_skips_file_without_a_parser(temp_dir: Path):
    events: list[str] = []
    path = temp_dir / "sample.xyz"
    path.write_text("hello", encoding="utf-8")
    pipeline = IngestionPipeline(
        sqlite_store=RecordingSQLite(events=events), semantic_memory=RecordingSemanticMemory(events=events)
    )
    result = await pipeline.ingest_file(path)
    assert result["status"] == "skipped"
    assert "No parser for extension" in result["error"]


async def test_doc_id_derives_from_path_and_content_hash(temp_dir: Path):
    events: list[str] = []
    path = make_markdown_file(temp_dir, "doc.md", "# 标题\n\n正文\n")
    pipeline = IngestionPipeline(
        sqlite_store=RecordingSQLite(events=events), semantic_memory=RecordingSemanticMemory(events=events)
    )
    pipeline.dispatcher = ParserDispatcher([RecordingParser(events)])
    pipeline.chunker = RecordingChunker(events)

    result = await pipeline.ingest_file(path)

    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = hashlib.sha256(f"{path}|{content_hash}".encode()).hexdigest()[:16]
    assert result["doc_id"] == expected
