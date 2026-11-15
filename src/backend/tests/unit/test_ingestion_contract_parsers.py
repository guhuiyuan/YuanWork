"""TASK-005 — what each parser actually produces.

Six parsers, six output contracts. The interesting part is not the
happy path but the edges:

* AC-F-011 — which block types and which metadata keys each parser emits
* AC-E-001 — an empty file: the text parser says "no content", the
  binary parsers raise
* AC-E-004 — the legacy binary extensions are registered as supported
  yet cannot be read
* AC-E-005 — a PDF page without a text layer yields a placeholder block,
  and OCR is never involved
* AC-E-011 — a PPTX table is stored twice, once inside the slide text
  and once as its own block
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.ingestion.parser.docx_parser import DocxParser
from yuanwork.ingestion.parser.md_txt_parser import MarkdownTxtParser
from yuanwork.ingestion.parser.pdf_parser import PDFParser
from yuanwork.ingestion.parser.pptx_parser import PptxParser
from yuanwork.ingestion.parser.xlsx_parser import XlsxParser
from unit.ingestion_fixtures import (
    make_docx_file,
    make_docx_with_table_file,
    make_docx_with_text_and_table_file,
    make_empty_file,
    make_markdown_file,
    make_pdf_file,
    make_pptx_file,
    make_scanned_pdf_file,
    make_text_file,
    make_xlsx_file,
)


# --------------------------------------------------------------------------
# AC-F-011 — per-parser output contracts
# --------------------------------------------------------------------------


async def test_pdf_emits_text_blocks_with_page_metadata(temp_dir: Path):
    blocks = await PDFParser().parse(make_pdf_file(temp_dir), "doc1")
    assert blocks
    for block in blocks:
        assert block.type == "text"
        assert {"page", "page_total", "is_scanned"} <= set(block.meta)
        assert block.meta["page"] >= 1
    assert "hello pdf" in blocks[0].content


async def test_docx_emits_text_blocks_with_heading_metadata(temp_dir: Path):
    blocks = await DocxParser().parse(
        make_docx_file(temp_dir, "b.docx", ["alpha", "beta"], heading="H"), "doc1"
    )
    assert blocks
    for block in blocks:
        assert block.type == "text"
        assert {"heading", "heading_level"} <= set(block.meta)
    assert "alpha" in blocks[0].content


async def test_docx_emits_table_blocks_for_tables(temp_dir: Path):
    blocks = await DocxParser().parse(make_docx_with_table_file(temp_dir), "doc1")
    table_blocks = [b for b in blocks if b.type == "table"]
    assert len(table_blocks) == 1
    assert "table_index" in table_blocks[0].meta
    assert "|" in table_blocks[0].content  # rendered as a markdown table


async def test_pptx_emits_text_blocks_with_slide_metadata(temp_dir: Path):
    blocks = await PptxParser().parse(make_pptx_file(temp_dir), "doc1")
    assert blocks
    for block in blocks:
        assert block.type == "text"
        assert {
            "slide_number",
            "slide_total",
            "slide_title",
            "has_notes",
            "has_tables",
        } <= set(block.meta)
        assert block.meta["slide_number"] == 1


async def test_xlsx_emits_row_blocks_with_sheet_metadata(temp_dir: Path):
    """One block per row, each with the sheet and the row number."""
    blocks = await XlsxParser().parse(make_xlsx_file(temp_dir), "doc1")
    assert len(blocks) == 2
    block = blocks[0]
    assert block.type == "table"
    assert {
        "sheet_name",
        "sheet_index",
        "sheet_total",
        "row_count",
        "row_index",
    } <= set(block.meta)
    assert block.meta["sheet_name"] == "进度表"
    assert block.meta["row_index"] == 1
    assert block.meta["anchor"] == {
        "kind": "sheet_row",
        "sheet": "进度表",
        "row": 1,
    }


async def test_markdown_emits_text_blocks_with_heading_metadata(temp_dir: Path):
    blocks = await MarkdownTxtParser().parse(
        make_markdown_file(temp_dir, "d.md", "# 标题\n\n正文\n"), "doc1"
    )
    assert blocks
    for block in blocks:
        assert block.type == "text"
        assert {"heading", "heading_level", "section_index"} <= set(block.meta)


async def test_plain_text_emits_one_block_per_paragraph(temp_dir: Path):
    path = make_text_file(temp_dir, "t.txt", "para one\n\npara two")
    blocks = await MarkdownTxtParser().parse(path, "doc1")
    assert len(blocks) == 2
    for block in blocks:
        assert block.type == "text"
        assert "paragraph_index" in block.meta


# --------------------------------------------------------------------------
# AC-E-001 — empty files
# --------------------------------------------------------------------------


async def test_empty_text_file_yields_no_blocks(temp_dir: Path):
    blocks = await MarkdownTxtParser().parse(make_empty_file(temp_dir, "e.md"), "doc1")
    assert blocks == []


@pytest.mark.parametrize(
    "parser,ext",
    [
        (PDFParser(), "pdf"),
        (DocxParser(), "docx"),
        (PptxParser(), "pptx"),
        (XlsxParser(), "xlsx"),
    ],
)
async def test_empty_binary_file_raises(parser, ext: str, temp_dir: Path):
    path = temp_dir / f"empty.{ext}"
    path.write_bytes(b"")
    with pytest.raises(Exception):
        await parser.parse(path, "doc1")


# --------------------------------------------------------------------------
# AC-E-004 — the legacy binary extensions
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parser,ext",
    [
        (DocxParser(), "doc"),
        (PptxParser(), "ppt"),
        (XlsxParser(), "xls"),
    ],
)
async def test_legacy_binary_extension_raises(parser, ext: str, temp_dir: Path):
    path = temp_dir / f"legacy.{ext}"
    path.write_bytes(b"legacy binary content")
    with pytest.raises(Exception):
        await parser.parse(path, "doc1")


# --------------------------------------------------------------------------
# AC-E-005 — a PDF page without a text layer
# --------------------------------------------------------------------------


async def test_scanned_pdf_page_yields_a_placeholder_block(temp_dir: Path):
    blocks = await PDFParser().parse(make_scanned_pdf_file(temp_dir), "doc1")
    assert len(blocks) == 1
    block = blocks[0]
    assert block.content == "[Scanned page - no text extracted]"
    assert block.meta["is_scanned"] is True
    # OCR is not part of the PDF path at all
    assert block.type == "text"
    assert "image_ocr" not in block.type


# --------------------------------------------------------------------------
# AC-E-011 — the PPTX table is stored twice
# --------------------------------------------------------------------------


async def test_pptx_table_content_is_kept_twice(temp_dir: Path):
    path = make_pptx_file(
        temp_dir,
        "deck.pptx",
        slides=[{"title": "T", "text": "body", "table": [["a", "b"], ["1", "2"]]}],
    )
    blocks = await PptxParser().parse(path, "doc1")

    text_blocks = [b for b in blocks if b.type == "text"]
    table_blocks = [b for b in blocks if b.type == "table"]
    assert len(text_blocks) == 1
    assert len(table_blocks) == 1

    # the table markdown is inside the slide text block *and* in the
    # dedicated table block
    assert "a | b" in text_blocks[0].content
    assert "a | b" in table_blocks[0].content


# --------------------------------------------------------------------------
# extra: the DOCX table loses its position in the document
# --------------------------------------------------------------------------


async def test_docx_table_block_comes_after_the_text_blocks(temp_dir: Path):
    path = make_docx_with_text_and_table_file(temp_dir)
    blocks = await DocxParser().parse(path, "doc1")
    types = [b.type for b in blocks]
    assert "table" in types
    assert types.index("table") == len(types) - 1
