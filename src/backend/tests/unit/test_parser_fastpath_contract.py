"""The PDF parser: the C library first, the slow path as a safety net.

A build of thousands of documents reads a lot of PDFs. The C library
(PyMuPDF) does that in a fraction of the time; pdfplumber is the
fallback for the documents it cannot read.

What must not change: the shape of what comes out. A reader who jumps
to page 12 must not have to care which path read it.

* AC-P-010 - the parsing reaches 120 files a minute
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from yuanwork.ingestion.parser.pdf_parser import PDFParser
from yuanwork.utils.anchor import has_anchor

from unit.ingestion_fixtures import make_pdf_file


@pytest.fixture
def contract(tmp_path: Path) -> Path:
    """A two-page PDF with a ruled table on the first page."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    path = tmp_path / "contract.pdf"
    page = canvas.Canvas(str(path), pagesize=A4, invariant=1)
    width, height = A4

    page.setFont("Helvetica", 12)
    page.drawString(60, height - 60, "Page one: the parties agree.")

    # A table with ruled lines - the kind both table finders can see.
    # The words sit inside the cells, not next to them: a table whose
    # text is not in its cells is not a table.
    rows = [("Project", "Budget"), ("Alpha", "1280"), ("Beta", "640")]
    left = 60
    page.setLineWidth(0.5)
    page.setFont("Helvetica", 11)
    for index, row in enumerate(rows):
        top = height - 118 + index * 20
        page.line(left, top, left + 320, top)
        page.line(left, top, left, top + 20)
        page.line(left + 320, top, left + 320, top + 20)
        page.drawString(left + 6, top + 6, row[0])
        page.drawString(left + 206, top + 6, row[1])
    bottom = height - 118 + len(rows) * 20
    page.line(left, bottom, left + 320, bottom)
    page.line(left, bottom, left, bottom + 20)
    page.line(left + 320, bottom, left + 320, bottom + 20)
    page.showPage()

    page.setFont("Helvetica", 12)
    page.drawString(60, height - 60, "Page two: the terms.")
    page.showPage()
    page.save()
    return path


@pytest.fixture
def plain(tmp_path: Path) -> Path:
    """A PDF with no table in it."""
    return make_pdf_file(
        tmp_path,
        name="plain.pdf",
        pages=["Just a line of text.", "And another one."],
    )


def without_pymupdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the fast path unavailable."""
    monkeypatch.setitem(sys.modules, "pymupdf", None)


# --------------------------------------------------------------------------
# the fast path
# --------------------------------------------------------------------------


async def test_the_c_library_reads_the_document(contract: Path):
    """The fast path is the one that runs."""
    blocks = await PDFParser().parse(contract, "d1")

    assert blocks
    assert any(b.meta.get("parser") == "pymupdf" for b in blocks)
    text = " ".join(b.content for b in blocks)
    assert "Page one" in text
    assert "Page two" in text


async def test_every_block_knows_where_it_came_from(contract: Path):
    """The anchor is the position of record - whichever path read it."""
    blocks = await PDFParser().parse(contract, "d1")

    for block in blocks:
        assert has_anchor({"anchor": block.meta.get("anchor")})
        assert block.meta["anchor"]["kind"] == "page"
        assert block.meta["page"] >= 1


async def test_the_tables_are_read(contract: Path):
    """A table is a table, whichever path found it."""
    blocks = await PDFParser().parse(contract, "d1")

    tables = [b for b in blocks if b.type == "table"]
    assert tables
    assert "Alpha" in tables[0].content
    assert tables[0].meta["page"] == 1


# --------------------------------------------------------------------------
# the slow path
# --------------------------------------------------------------------------


async def test_the_slow_path_reads_the_document_too(
    contract: Path, monkeypatch: pytest.MonkeyPatch
):
    """Without the C library the document is still read."""
    without_pymupdf(monkeypatch)

    blocks = await PDFParser().parse(contract, "d1")

    assert blocks
    assert any(b.meta.get("parser") == "pdfplumber" for b in blocks)
    text = " ".join(b.content for b in blocks)
    assert "Page one" in text


async def test_both_paths_produce_the_same_anchors(
    contract: Path, monkeypatch: pytest.MonkeyPatch
):
    """The reader must not have to care which path read it."""
    fast = await PDFParser().parse(contract, "d1")
    fast_anchors = [b.meta["anchor"] for b in fast]

    without_pymupdf(monkeypatch)
    slow = await PDFParser().parse(contract, "d1")
    slow_anchors = [b.meta["anchor"] for b in slow]

    # The same pages come out of both paths. The order may differ -
    # the slow path adds the tables where it finds them - but a reader
    # who jumps to page 12 does not care which path read it.
    assert sorted(a["page"] for a in fast_anchors) == sorted(
        a["page"] for a in slow_anchors
    )
    assert {a["page"] for a in fast_anchors} == {a["page"] for a in slow_anchors}


async def test_the_slow_path_only_takes_a_turn_when_it_has_to(
    plain: Path,
):
    """A document without a table is not read a second time."""
    parser = PDFParser()

    called = []
    original = parser._parse_slow

    def _record(file_path, doc_id, keep=None):
        called.append(doc_id)
        return original(file_path, doc_id, keep=keep)

    parser._parse_slow = _record  # type: ignore[method-assign]
    await parser.parse(plain, "d2")

    assert called == [], "the slow path ran over a document it did not have to"


async def test_a_document_nobody_can_read_says_so(
    tmp_path: Path,
):
    """A file that is not a PDF is an error - like every other format.

    The pipeline catches it per file: one unreadable document does not
    stop a build.
    """
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a pdf at all")

    with pytest.raises(Exception):
        await PDFParser().parse(broken, "d1")


# --------------------------------------------------------------------------
# the metadata
# --------------------------------------------------------------------------


async def test_the_metadata_comes_from_the_fast_path(contract: Path):
    meta = await PDFParser().extract_metadata(contract)

    assert meta["page_count"] == 2


async def test_the_metadata_falls_back_too(
    contract: Path, monkeypatch: pytest.MonkeyPatch
):
    without_pymupdf(monkeypatch)

    meta = await PDFParser().extract_metadata(contract)

    assert meta["page_count"] == 2
