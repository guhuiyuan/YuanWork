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
    make_docx_with_image_file,
    make_docx_with_textbox_file,
    make_docx_with_table_file,
    make_docx_with_text_and_table_file,
    make_empty_file,
    make_markdown_file,
    make_pdf_file,
    make_pptx_file,
    make_pptx_with_image_file,
    make_pptx_with_object_placeholder_file,
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


# --------------------------------------------------------------------------
# AC-F-002 — a supported format is never lost to one odd shape
# --------------------------------------------------------------------------


async def test_pptx_with_an_object_placeholder_is_still_read(temp_dir: Path):
    """A slide whose "title" is an embedded object, not a text shape.

    PowerPoint puts equation objects into a placeholder with no type and
    no index; python-pptx hands that object back as the title, and it has
    no text at all. Asking it for `.text` used to raise and the whole
    .pptx was lost - the file is a supported format, so it has to be
    read.
    """
    path = make_pptx_with_object_placeholder_file(temp_dir)
    blocks = await PptxParser().parse(path, "doc1")

    assert blocks
    text_blocks = [b for b in blocks if b.type == "text"]
    assert len(text_blocks) == 1
    # The slide has no title (the title is an object), but its text is
    # still there, and the position still points at the slide.
    assert "公式对象" in text_blocks[0].content
    assert text_blocks[0].meta["slide_number"] == 1
    assert text_blocks[0].meta["anchor"]["kind"] == "slide"
    assert text_blocks[0].meta["slide_title"] == ""


async def test_a_docx_whose_text_is_in_a_text_box_is_read(temp_dir: Path):
    """The text in a text box is the document's text.

    Word's experiment-report templates put the whole document in
    floating text boxes: the body paragraphs are empty spacers and the
    visible text is inside `<w:txbxContent>`, which python-docx does
    not read. The document used to come out with **zero** blocks and be
    dropped as "no content" - silently.
    """
    path = make_docx_with_textbox_file(temp_dir)
    blocks = await DocxParser().parse(path, "doc1")

    assert blocks
    text = "\n".join(b.content for b in blocks)
    assert "测定一阶电路的时间常数" in text
    # The block says it came from a text box, so the position can be
    # traced back to the document.
    assert any(b.meta.get("in_textbox") for b in blocks)
    assert all(b.meta.get("anchor") for b in blocks)


# --------------------------------------------------------------------------
# AC-E-004 — a scan: the body of the document is a picture
# --------------------------------------------------------------------------


async def test_a_docx_that_is_a_scan_is_read_by_ocr(temp_dir: Path):
    """A scanned document is a picture with a file name.

    There is not one paragraph of text in it - the words are in the
    image. Only OCR finds them; without it the file is dropped as "no
    content" and the user never sees it.
    """
    pytest.importorskip("rapidocr_onnxruntime")

    path = make_docx_with_image_file(temp_dir)
    blocks = await DocxParser().parse(path, "doc1")

    assert blocks
    ocr_blocks = [b for b in blocks if b.type == "image_ocr"]
    assert ocr_blocks, f"the scan was not read: {[b.type for b in blocks]}"

    text = " ".join(b.content for b in ocr_blocks)
    assert "实验报告" in text

    # the picture keeps its place, so the citation can point at it
    assert all(b.meta.get("in_image") for b in ocr_blocks)
    assert all(b.meta.get("anchor") for b in ocr_blocks)


async def test_a_pptx_whose_slide_is_a_scan_is_read_by_ocr(temp_dir: Path):
    """The same for a deck: a slide can be a picture of a slide."""
    pytest.importorskip("rapidocr_onnxruntime")

    path = make_pptx_with_image_file(temp_dir)
    blocks = await PptxParser().parse(path, "doc1")

    assert blocks
    ocr_blocks = [b for b in blocks if b.type == "image_ocr"]
    assert ocr_blocks, f"the scan was not read: {[b.type for b in blocks]}"

    text = " ".join(b.content for b in ocr_blocks)
    assert "实验报告" in text
    # the slide it is on is the position
    assert all(b.meta.get("slide_number") == 1 for b in ocr_blocks)
    assert all(b.meta.get("anchor") for b in ocr_blocks)


async def test_a_scan_that_cannot_be_read_says_why(temp_dir: Path, monkeypatch):
    """No OCR engine: the file must not vanish.

    AC-E-004 - a file that cannot be read is listed with the reason.
    Saying "no content" would be a lie: the document has plenty of
    content, it is a picture of it.
    """
    from yuanwork.ingestion.parser import ocr

    monkeypatch.setattr(ocr, "get_engine", lambda: None)
    monkeypatch.setattr(ocr, "engine_error", lambda: "engine not installed")

    path = make_docx_with_image_file(temp_dir)
    blocks = await DocxParser().parse(path, "doc1")

    assert blocks
    placeholder = [b for b in blocks if b.type == "image_placeholder"]
    assert len(placeholder) == 1
    assert "图片" in placeholder[0].content
    assert "engine not installed" in placeholder[0].content
    # the position is still there - the file is in the index
    assert placeholder[0].meta.get("anchor")


async def test_a_logo_is_not_read(temp_dir: Path):
    """A picture the size of an icon has no text worth reading.

    Reading it would be slow, and what came out would be noise. The
    file still has to say what happened to it.
    """
    path = make_docx_with_image_file(
        temp_dir, name="logo.docx", text="X", width=32, height=32
    )
    blocks = await DocxParser().parse(path, "doc1")

    assert blocks
    assert not [b for b in blocks if b.type == "image_ocr"]
    placeholder = [b for b in blocks if b.type == "image_placeholder"]
    assert len(placeholder) == 1
    assert "太小" in placeholder[0].content
