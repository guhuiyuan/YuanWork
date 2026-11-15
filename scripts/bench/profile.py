"""The shape of a real workspace - counted, not read.

The synthetic corpus says what a build costs on a corpus with a known
shape. The real workspace says what the user's own documents look like:
how many pages, paragraphs, tables and pictures, and how big the files
are.

The rule here is that the profiler **does not read the documents**. It
counts their structure and nothing else - no text, no content, no
snippets of the original. A profile is a number to compare against,
not a copy of the user's material, and it does not belong on disk
either.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

#: The file kinds the profiler knows.
_KINDS = {
    ".pdf": "pdf",
    ".pptx": "pptx",
    ".docx": "docx",
    ".xlsx": "xlsx",
    ".md": "md",
    ".txt": "txt",
    ".png": "png",
    ".jpg": "png",
    ".jpeg": "png",
}


def _pdf_pages(path: Path) -> int:
    """How many pages a PDF has.

    The page tree is read - not a single word of the text.
    """
    try:
        import pdfplumber

        with pdfplumber.open(path) as pdf:
            return len(pdf.pages)
    except Exception:
        return 0


def _docx_structure(path: Path) -> tuple[int, int]:
    """The paragraphs and the tables of a Word document.

    The counts come from the document's XML - the words are not read.
    """
    try:
        from docx import Document

        document = Document(str(path))
        return len(document.paragraphs), len(document.tables)
    except Exception:
        return 0, 0


def _pptx_slides(path: Path) -> int:
    try:
        from pptx import Presentation

        return len(Presentation(str(path)).slides)
    except Exception:
        return 0


def _xlsx_structure(path: Path) -> tuple[int, int]:
    """The sheets and the rows of a workbook."""
    try:
        from openpyxl import load_workbook

        workbook = load_workbook(str(path), read_only=True)
        sheets = len(workbook.sheetnames)
        rows = 0
        for sheet in workbook.worksheets:
            rows += max(0, (sheet.max_row or 0))
        workbook.close()
        return sheets, rows
    except Exception:
        return 0, 0


def _markdown_lines(path: Path) -> int:
    """How many lines a note has - counted, not read into memory."""
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except Exception:
        return 0


def profile_workspace(root: Path) -> dict[str, Any]:
    """The shape of the workspace under ``root``.

    What it counts: the files by kind, the pages, the paragraphs, the
    tables, the share of pictures, and how the sizes are distributed.
    What it does not do: read a word of the documents, or write any of
    it down.
    """
    root = Path(root)
    empty: dict[str, Any] = {
        "root": str(root),
        "exists": root.exists(),
        "files": 0,
        "counts": {},
        "pages": 0,
        "paragraphs": 0,
        "tables": 0,
        "image_ratio": 0.0,
        "total_bytes": 0,
        "size_bytes": {"min": 0, "p50": 0, "p90": 0, "max": 0},
    }
    if not root.exists():
        return empty

    counts: dict[str, int] = {}
    pages = 0
    paragraphs = 0
    tables = 0
    sizes: list[int] = []
    total_bytes = 0
    files = 0

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        kind = _KINDS.get(path.suffix.lower())
        if kind is None:
            continue
        files += 1
        counts[kind] = counts.get(kind, 0) + 1
        size = path.stat().st_size
        sizes.append(size)
        total_bytes += size

        if kind == "pdf":
            pages += _pdf_pages(path)
        elif kind == "pptx":
            pages += _pptx_slides(path)
        elif kind == "docx":
            doc_paragraphs, doc_tables = _docx_structure(path)
            paragraphs += doc_paragraphs
            tables += doc_tables
        elif kind == "xlsx":
            sheets, rows = _xlsx_structure(path)
            pages += sheets
            paragraphs += rows
        elif kind in ("md", "txt"):
            paragraphs += _markdown_lines(path)

    sizes.sort()

    def _percentile(fraction: float) -> int:
        if not sizes:
            return 0
        index = min(len(sizes) - 1, int(len(sizes) * fraction))
        return sizes[index]

    return {
        "root": str(root),
        "exists": True,
        "files": files,
        "counts": counts,
        "pages": pages,
        "paragraphs": paragraphs,
        "tables": tables,
        "image_ratio": round(counts.get("png", 0) / files, 4) if files else 0.0,
        "total_bytes": total_bytes,
        "size_bytes": {
            "min": sizes[0] if sizes else 0,
            "p50": _percentile(0.5),
            "p90": _percentile(0.9),
            "max": sizes[-1] if sizes else 0,
        },
    }
