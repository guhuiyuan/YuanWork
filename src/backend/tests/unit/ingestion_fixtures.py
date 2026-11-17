"""Shared sample builders for the ingestion contract tests.

These build *real* files on disk so the parsers run their actual code
paths — no mocking of the document libraries. Every builder is
deterministic: calling it twice with the same arguments produces the same
bytes, which is what the identity/hash tests rely on.

Import from sibling test modules with::

    from unit.ingestion_fixtures import make_pdf_file

(pytest puts ``src/backend/tests`` on sys.path because ``unit/`` is a
package and ``tests/`` is not.)

It also holds the test doubles the pipeline tests need: the pipeline only
touches four collaborators, and stubbing them keeps the contract tests
free of a database, a vector store and an embedding model.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from yuanwork.ingestion.parser.base import BaseParser, RawBlock

# --------------------------------------------------------------------------
# plain files
# --------------------------------------------------------------------------


def write_bytes(path: Path, data: bytes) -> Path:
    """Write raw bytes and return the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def make_empty_file(dir_path: Path, name: str = "empty.txt") -> Path:
    """A 0-byte file."""
    return write_bytes(dir_path / name, b"")


def make_text_file(
    dir_path: Path,
    name: str = "doc.txt",
    content: str = "hello world",
    encoding: str = "utf-8",
) -> Path:
    """A text file with the given encoding."""
    path = dir_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=encoding)
    return path


def make_markdown_file(
    dir_path: Path,
    name: str = "doc.md",
    content: str = "# Title\n\nbody text\n",
) -> Path:
    """A Markdown file (UTF-8, no BOM)."""
    return make_text_file(dir_path, name, content)


def make_binary_file(
    dir_path: Path,
    name: str = "binary.txt",
    data: bytes = b"\x00\x01\x02\xff\xfe\x80\x81",
) -> Path:
    """A file whose bytes are NOT valid UTF-8.

    Used to pin the GBK-with-errors-ignore fallback in the text parser.
    """
    return write_bytes(dir_path / name, data)


# --------------------------------------------------------------------------
# PDF (hand-rolled — no PDF writer is available in the venv)
# --------------------------------------------------------------------------


def _pdf_bytes(pages: list[str | None]) -> bytes:
    """Build a minimal but valid PDF.

    ``pages`` holds one entry per page: a string becomes a text object,
    ``None`` becomes a page with no text layer at all (the "scanned page"
    case the PDF parser is expected to detect).
    """
    n_pages = len(pages)
    page_ids = [3 + i for i in range(n_pages)]
    content_ids = [3 + n_pages + i for i in range(n_pages)]
    font_id = 3 + 2 * n_pages

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{pid} 0 R" for pid in page_ids)
            + f"] /Count {n_pages} >>"
        ).encode(),
    ]
    for i in range(n_pages):
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {content_ids[i]} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
    for text in pages:
        if text is None:
            stream = b""
        else:
            escaped = (
                text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
            )
            stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode()
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_off = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_off}\n%%EOF\n"
    ).encode()
    return bytes(out)


def make_pdf_file(
    dir_path: Path,
    name: str = "doc.pdf",
    pages: list[str | None] | None = None,
) -> Path:
    """A PDF. ``pages=None`` means one page carrying ``hello pdf``."""
    if pages is None:
        pages = ["hello pdf"]
    return write_bytes(dir_path / name, _pdf_bytes(pages))


def make_scanned_pdf_file(dir_path: Path, name: str = "scanned.pdf") -> Path:
    """A one-page PDF with no text layer (the 'scanned page' case)."""
    return make_pdf_file(dir_path, name, pages=[None])


# --------------------------------------------------------------------------
# Office formats
# --------------------------------------------------------------------------


def make_docx_file(
    dir_path: Path,
    name: str = "doc.docx",
    paragraphs: Iterable[str] = ("first paragraph", "second paragraph"),
    heading: str | None = "Chapter One",
) -> Path:
    """A .docx with an optional heading followed by paragraphs."""
    from docx import Document

    path = dir_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    if heading is not None:
        doc.add_heading(heading, level=1)
    for para in paragraphs:
        doc.add_paragraph(para)
    doc.save(path)
    return path


def make_docx_with_table_file(
    dir_path: Path,
    name: str = "table.docx",
    rows: list[list[str]] | None = None,
) -> Path:
    """A .docx containing a table."""
    from docx import Document

    if rows is None:
        rows = [["项目", "预算"], ["一期", "1280"]]

    path = dir_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading("项目进度", level=1)
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    doc.save(path)
    return path


def make_docx_with_text_and_table_file(
    dir_path: Path,
    name: str = "mixed.docx",
) -> Path:
    """A .docx with paragraphs *and* a table.

    Used to pin the ordering: the DOCX parser emits every paragraph
    block first and the table block afterwards, so the table loses its
    position in the document.
    """
    from docx import Document

    path = dir_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = Document()
    doc.add_heading("章节标题", level=1)
    doc.add_paragraph("段落一")
    doc.add_paragraph("段落二")
    table = doc.add_table(rows=2, cols=2)
    for r, row in enumerate([["项目", "预算"], ["一期", "1280"]]):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    doc.save(path)
    return path


def make_xlsx_file(
    dir_path: Path,
    name: str = "sheet.xlsx",
    sheets: dict[str, list[list[Any]]] | None = None,
) -> Path:
    """A .xlsx. ``sheets`` maps sheet name -> rows."""
    from openpyxl import Workbook

    if sheets is None:
        sheets = {"进度表": [["项目", "预算"], ["一期", 1280]]}

    path = dir_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.remove(wb.active)
    for sheet_name, rows in sheets.items():
        ws = wb.create_sheet(title=sheet_name)
        for row in rows:
            ws.append(row)
    wb.save(path)
    return path


def make_pptx_file(
    dir_path: Path,
    name: str = "deck.pptx",
    slides: list[dict[str, Any]] | None = None,
) -> Path:
    """A .pptx. Each slide is ``{"title": str, "text": str, "table": rows}``."""
    from pptx import Presentation
    from pptx.util import Inches

    if slides is None:
        slides = [{"title": "产品路线图", "text": "核心功能介绍"}]

    path = dir_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    prs = Presentation()
    for spec in slides:
        slide = prs.slides.add_slide(prs.slide_layouts[5])
        slide.shapes.title.text = spec.get("title", "")
        if spec.get("text"):
            box = slide.shapes.add_textbox(
                Inches(1), Inches(2), Inches(6), Inches(3)
            )
            box.text_frame.text = spec["text"]
        table_rows = spec.get("table")
        if table_rows:
            shape = slide.shapes.add_table(
                len(table_rows), len(table_rows[0]), Inches(1), Inches(4), Inches(6), Inches(2)
            )
            for r, row in enumerate(table_rows):
                for c, value in enumerate(row):
                    shape.table.cell(r, c).text = str(value)
    prs.save(path)
    return path


# --------------------------------------------------------------------------
# images
# --------------------------------------------------------------------------

_CJK_FONTS = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
)


def _load_cjk_font(size: int = 32):
    from PIL import ImageFont

    for fp in _CJK_FONTS:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()


def make_image_file(
    dir_path: Path,
    name: str = "shot.png",
    lines: list[str] | None = None,
    size: tuple[int, int] = (640, 120),
) -> Path:
    """A PNG with the given lines rendered onto it."""
    from PIL import Image, ImageDraw

    if lines is None:
        lines = ["测试 OCR"]

    path = dir_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = _load_cjk_font()
    y = 10
    for line in lines:
        draw.text((20, y), line, fill="black", font=font)
        y += 50
    img.save(path)
    return path


def make_corrupt_image_file(dir_path: Path, name: str = "broken.png") -> Path:
    """A file with an image extension whose bytes are not an image."""
    return write_bytes(dir_path / name, b"\x89PNG\r\n\x1a\n" + b"garbage" * 8)


def make_exif_image_file(dir_path: Path, name: str = "rotated.jpg") -> Path:
    """A JPEG carrying an EXIF Orientation tag of 6 (rotate 90°).

    The parser is expected to record the *pre-rotation* dimensions.
    """
    from PIL import Image

    path = dir_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (200, 100), "white")
    exif = img.getexif()
    exif[274] = 6  # Orientation
    img.save(path, exif=exif)
    return path


# --------------------------------------------------------------------------
# test doubles for the pipeline's collaborators
# --------------------------------------------------------------------------


class RecordingSQLite:
    """Stand-in for the file-metadata store.

    ``existing`` is what a lookup by content hash should return; set it
    to simulate "this content is already indexed". A record that has no
    ``chunk_version`` counts as "cut with unknown rules" - the index
    then cuts it again (ADR-046).
    """

    def __init__(self, existing: dict | None = None, events: list[str] | None = None):
        self.existing = existing
        self.events = events if events is not None else []
        self.upserts: list[dict] = []
        self.hash_lookups: list[str] = []

    async def get_file_by_hash(self, file_hash: str) -> dict | None:
        self.events.append("hash_lookup")
        self.hash_lookups.append(file_hash)
        if self.existing is None:
            return None
        found = dict(self.existing)
        found.setdefault("chunk_version", "recording-v1")
        return found

    async def upsert_file(self, **kwargs) -> str:
        self.events.append("upsert")
        self.upserts.append(kwargs)
        return kwargs.get("doc_id", "")


class RecordingSemanticMemory:
    """Stand-in for the vector store + full-text index."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.events = events if events is not None else []
        self.added: list[dict] = []

    async def add_chunks(self, chunks: list[dict]) -> list[str]:
        self.events.append("embed")
        self.added.extend(chunks)
        return [c["id"] for c in chunks]


class RecordingParser(BaseParser):
    """A parser that returns one fixed block and records its calls."""

    SUPPORTED_EXTENSIONS = ("md",)

    def __init__(self, events: list[str] | None = None, content: str = "正文内容" * 10):
        self.events = events if events is not None else []
        self.content = content

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        self.events.append("parse")
        return [
            RawBlock(
                block_id=f"{doc_id}_b1",
                doc_id=doc_id,
                content=self.content,
                type="text",
                meta={"page": 3},
            )
        ]

    async def extract_metadata(self, file_path: Path) -> dict[str, Any]:
        self.events.append("metadata")
        return {"size": 1}


class RecordingChunker:
    """A chunker that returns one fixed chunk and records its call."""

    #: Which rules it cuts with - the index compares it, so that a
    #: change of the rules cuts the file again (ADR-046).
    version = "recording-v1"

    def __init__(self, events: list[str] | None = None):
        self.events = events if events is not None else []

    def chunk(self, blocks: list[RawBlock], doc_id: str) -> list[dict[str, Any]]:
        self.events.append("chunk")
        return [
            {
                "id": "chunk-1",
                "doc_id": doc_id,
                "chunk_idx": 1,
                "text": blocks[0].content,
                "type": blocks[0].type,
                "meta": dict(blocks[0].meta),
            }
        ]
