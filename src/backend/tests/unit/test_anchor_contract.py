"""TASK-002 — one anchor structure for every kind of position.

The product promise is *能定位*: an answer has to point at the exact
place in the original document. That only works if every format says
*where* in the same way - and if a piece of evidence that cannot be
placed is recognised as such instead of being quietly used.

* AC-F-003 — the five kinds of position: page, slide, section, sheet+row,
  heading path
* AC-E-007 — evidence without a position is not locatable
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yuanwork.ingestion.parser.docx_parser import DocxParser
from yuanwork.ingestion.parser.md_txt_parser import MarkdownTxtParser
from yuanwork.ingestion.parser.pdf_parser import PDFParser
from yuanwork.ingestion.parser.pptx_parser import PptxParser
from yuanwork.ingestion.parser.xlsx_parser import XlsxParser
from yuanwork.utils.anchor import (
    ANCHOR_KINDS,
    anchor_location,
    anchor_of,
    has_anchor,
    make_anchor,
)
from yuanwork.utils.chunk_meta import chunk_location, chunk_meta
from unit.ingestion_fixtures import (
    make_docx_file,
    make_markdown_file,
    make_pdf_file,
    make_pptx_file,
    make_xlsx_file,
)


# --------------------------------------------------------------------------
# AC-F-003 — the five kinds
# --------------------------------------------------------------------------


async def test_a_pdf_page_carries_a_page_anchor(temp_dir: Path):
    blocks = await PDFParser().parse(
        make_pdf_file(temp_dir, "doc.pdf", pages=["第一页", "第二页"]), "doc1"
    )

    assert blocks
    anchors = [anchor_of({"meta": b.meta}) for b in blocks]
    assert all(a is not None for a in anchors)
    assert {a["kind"] for a in anchors} == {"page"}
    assert [a["page"] for a in anchors] == [1, 2]


async def test_a_slide_carries_a_slide_anchor(temp_dir: Path):
    blocks = await PptxParser().parse(make_pptx_file(temp_dir, "deck.pptx"), "doc1")

    assert blocks
    for block in blocks:
        anchor = anchor_of({"meta": block.meta})
        assert anchor is not None
        assert anchor["kind"] == "slide"
        assert anchor["slide"] == block.meta["slide_number"]


async def test_a_word_section_carries_a_section_anchor(temp_dir: Path):
    blocks = await DocxParser().parse(
        make_docx_file(temp_dir, "doc.docx", paragraphs=("正文一段",), heading="第一章"),
        "doc1",
    )

    assert blocks
    anchor = anchor_of({"meta": blocks[0].meta})
    assert anchor is not None
    assert anchor["kind"] == "section"
    assert anchor["section"] == "第一章"
    assert anchor["heading_path"] == ["第一章"]


async def test_a_spreadsheet_row_carries_a_sheet_and_a_row(temp_dir: Path):
    blocks = await XlsxParser().parse(
        make_xlsx_file(
            temp_dir,
            "sheet.xlsx",
            sheets={"进度表": [["项目", "预算"], ["一期", 1280], ["二期", 2560]]},
        ),
        "doc1",
    )

    assert blocks
    rows = []
    for block in blocks:
        anchor = anchor_of({"meta": block.meta})
        assert anchor is not None
        assert anchor["kind"] == "sheet_row"
        assert anchor["sheet"] == "进度表"
        rows.append(anchor["row"])

    # every row of the sheet is addressable, and the row numbers are the
    # ones the spreadsheet itself uses
    assert rows == [1, 2, 3]
    assert "一期" in blocks[1].content
    assert "项目" in blocks[1].content  # the header travels with the row


async def test_a_markdown_section_carries_its_heading_path(temp_dir: Path):
    path = make_markdown_file(
        temp_dir,
        "d.md",
        "# 第一章\n\n正文\n\n## 1.1 概述\n\n更多正文\n",
    )
    blocks = await MarkdownTxtParser().parse(path, "doc1")

    assert len(blocks) == 2
    first = anchor_of({"meta": blocks[0].meta})
    assert first is not None
    assert first["kind"] == "heading_path"
    assert first["heading_path"] == ["第一章"]

    second = anchor_of({"meta": blocks[1].meta})
    assert second is not None
    assert second["heading_path"] == ["第一章", "1.1 概述"]


def test_the_five_kinds_are_the_ones_the_spec_lists():
    assert ANCHOR_KINDS == ("page", "slide", "section", "sheet_row", "heading_path")


def test_an_anchor_keeps_its_coordinates_and_the_extra_ones():
    anchor = make_anchor("page", page=12, line=3)
    assert anchor["kind"] == "page"
    assert anchor["page"] == 12
    # a few formats know a finer position than their kind names - an image
    # knows the line inside it - so extra coordinates travel along
    assert anchor["line"] == 3


def test_an_anchor_drops_the_coordinates_that_are_not_set():
    anchor = make_anchor("sheet_row", sheet="台账", row=None)
    assert anchor == {"kind": "sheet_row", "sheet": "台账"}


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        make_anchor("chapter", page=1)


# --------------------------------------------------------------------------
# the anchor survives the round trip
# --------------------------------------------------------------------------


def test_the_anchor_reaches_the_flat_fields():
    chunk = {"meta": {"anchor": make_anchor("sheet_row", sheet="台账", row=7)}}

    meta = chunk_meta(chunk)

    assert meta["sheet"] == "台账"
    assert meta["row"] == 7
    assert meta["anchor"]["kind"] == "sheet_row"


def test_an_indexed_row_reads_its_anchor_back():
    """The column in the database is a JSON string."""
    chunk = {"anchor": json.dumps(make_anchor("slide", slide=3), ensure_ascii=False)}

    anchor = anchor_of(chunk)

    assert anchor is not None
    assert anchor["kind"] == "slide"
    assert anchor["slide"] == 3


def test_a_row_indexed_before_the_anchor_still_says_where_it_is():
    """The old columns are the fallback, so old rows keep working."""
    chunk = {"page": 12}

    anchor = anchor_of(chunk)

    assert anchor is not None
    assert anchor["kind"] == "page"
    assert anchor["page"] == 12


def test_the_location_is_readable_for_every_kind():
    assert anchor_location(make_anchor("page", page=12)) == "第 12 页"
    assert anchor_location(make_anchor("slide", slide=3)) == "Slide 3"
    assert anchor_location(make_anchor("section", section="第三章 费用")) == "第三章 费用"
    assert (
        anchor_location(make_anchor("sheet_row", sheet="台账", row=7)) == "台账 第 7 行"
    )
    assert (
        anchor_location(make_anchor("heading_path", heading_path=["第一章", "1.1"]))
        == "第一章 › 1.1"
    )


def test_the_chunk_location_reads_the_anchor():
    chunk = {"meta": {"anchor": make_anchor("page", page=9)}}
    assert chunk_location(chunk) == "第 9 页"


# --------------------------------------------------------------------------
# AC-E-007 — evidence that cannot be placed
# --------------------------------------------------------------------------


def test_a_chunk_without_a_position_is_not_locatable():
    chunk = {"id": "c1", "text": "一段没有出处的文字"}

    assert anchor_of(chunk) is None
    assert has_anchor(chunk) is False


def test_a_chunk_with_a_position_is_locatable():
    chunk = {"id": "c1", "text": "有出处的文字", "meta": {"anchor": make_anchor("page", page=2)}}

    assert has_anchor(chunk) is True


def test_an_anchor_without_its_coordinate_is_not_locatable():
    """A kind that carries no coordinate pins down nothing."""
    chunk = {"meta": {"anchor": {"kind": "page"}}}

    assert has_anchor(chunk) is False
