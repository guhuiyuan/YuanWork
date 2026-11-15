"""TASK-001 — the shared ingestion sample builders themselves.

The rest of the ingestion contract tests lean on these builders, so they
need their own guarantee: every builder produces a real, parseable file
and produces the *same* file every time it is called with the same
arguments.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from unit.ingestion_fixtures import (
    make_binary_file,
    make_corrupt_image_file,
    make_docx_file,
    make_docx_with_table_file,
    make_empty_file,
    make_exif_image_file,
    make_image_file,
    make_markdown_file,
    make_pdf_file,
    make_pptx_file,
    make_scanned_pdf_file,
    make_text_file,
    make_xlsx_file,
)


def test_text_file_is_readable_and_deterministic(temp_dir: Path):
    a = make_text_file(temp_dir / "a", "doc.txt", "hello")
    b = make_text_file(temp_dir / "b", "doc.txt", "hello")
    assert a.read_text(encoding="utf-8") == "hello"
    assert a.read_bytes() == b.read_bytes()


def test_empty_file_has_zero_bytes(temp_dir: Path):
    path = make_empty_file(temp_dir)
    assert path.stat().st_size == 0


def test_markdown_file_round_trips(temp_dir: Path):
    path = make_markdown_file(temp_dir, "doc.md", "# 标题\n\n正文\n")
    assert path.read_text(encoding="utf-8").startswith("# 标题")


def test_binary_file_is_not_valid_utf8(temp_dir: Path):
    path = make_binary_file(temp_dir)
    with pytest.raises(UnicodeDecodeError):
        path.read_text(encoding="utf-8")


def test_pdf_file_is_parseable_and_carries_its_text(temp_dir: Path):
    pdfplumber = pytest.importorskip("pdfplumber")
    path = make_pdf_file(temp_dir, "doc.pdf", pages=["hello pdf"])
    with pdfplumber.open(path) as pdf:
        assert len(pdf.pages) == 1
        assert "hello pdf" in (pdf.pages[0].extract_text() or "")


def test_scanned_pdf_has_a_page_without_text(temp_dir: Path):
    pdfplumber = pytest.importorskip("pdfplumber")
    path = make_scanned_pdf_file(temp_dir)
    with pdfplumber.open(path) as pdf:
        assert len(pdf.pages) == 1
        assert not (pdf.pages[0].extract_text() or "").strip()


def test_docx_file_is_readable(temp_dir: Path):
    docx = pytest.importorskip("docx")
    path = make_docx_file(temp_dir, "doc.docx", ["alpha", "beta"], heading="H")
    doc = docx.Document(path)
    assert [p.text for p in doc.paragraphs if p.text] == ["H", "alpha", "beta"]


def test_docx_with_table_has_a_table(temp_dir: Path):
    docx = pytest.importorskip("docx")
    path = make_docx_with_table_file(temp_dir)
    doc = docx.Document(path)
    assert len(doc.tables) == 1
    assert doc.tables[0].cell(0, 0).text == "项目"


def test_xlsx_file_is_readable(temp_dir: Path):
    openpyxl = pytest.importorskip("openpyxl")
    path = make_xlsx_file(temp_dir)
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["进度表"]
    assert wb["进度表"].cell(2, 2).value == 1280


def test_pptx_file_is_readable(temp_dir: Path):
    pptx = pytest.importorskip("pptx")
    path = make_pptx_file(temp_dir)
    prs = pptx.Presentation(path)
    assert len(prs.slides) == 1


def test_image_file_has_the_requested_size(temp_dir: Path):
    pytest.importorskip("PIL")
    path = make_image_file(temp_dir, "shot.png", ["hello"], size=(320, 80))
    from PIL import Image

    with Image.open(path) as img:
        assert img.size == (320, 80)


def test_corrupt_image_file_is_not_an_image(temp_dir: Path):
    pytest.importorskip("PIL")
    path = make_corrupt_image_file(temp_dir)
    from PIL import Image

    with pytest.raises(Exception):
        Image.open(path)


def test_exif_image_file_carries_an_orientation_tag(temp_dir: Path):
    pytest.importorskip("PIL")
    path = make_exif_image_file(temp_dir)
    from PIL import Image

    with Image.open(path) as img:
        assert img.getexif().get(274) == 6
        assert img.size == (200, 100)
