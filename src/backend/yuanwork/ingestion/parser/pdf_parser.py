"""PDF parser: the C library first, the slow path as a safety net.

Two paths, one shape:

* the **fast path** is PyMuPDF (the MuPDF C library). It reads the
  text and the tables of a PDF in a fraction of the time pdfplumber
  needs, and it is what a build of thousands of documents runs on
  (ADR-036).
* the **slow path** is pdfplumber. It is the fallback for the documents
  the fast path cannot read - a layout MuPDF does not understand, a
  page whose text comes out empty where there clearly is some.

Both paths produce the same anchors: a page is a page, wherever it
came from. A reader who can jump to page 12 of a document must not
have to care which path read it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from yuanwork.ingestion.parser.base import BaseParser, RawBlock
from yuanwork.utils.anchor import make_anchor

logger = logging.getLogger(__name__)

#: Two words with a run of spaces between them - a column, most likely.
_COLUMN_GAP = re.compile(r"\S {2,}\S")


class PDFParser(BaseParser):
    """Parse PDF documents, preserving page-level metadata."""

    SUPPORTED_EXTENSIONS = ("pdf",)

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Parse PDF into page-level blocks."""
        return await asyncio.to_thread(self._parse_sync, file_path, doc_id)

    def _parse_sync(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Synchronous PDF parsing (runs in thread)."""
        fast = self._parse_fast(file_path, doc_id)
        if fast is None:
            # PyMuPDF is not there, or it could not open the document.
            return self._parse_slow(file_path, doc_id)

        blocks, table_pages = fast

        if self._needs_slow_path(blocks, table_pages):
            # The fast path found the pages but not what is in them -
            # a layout MuPDF does not read, or a table it did not stop
            # at. The slow path gets a turn.
            try:
                blocks = self._parse_slow(file_path, doc_id, keep=blocks)
            except Exception as e:  # noqa: BLE001 - the fast path's answer stands
                logger.warning(f"The slow path could not read {file_path}: {e!r}")

        return blocks

    # --------------------------------------------------------------------------
    # the fast path - the C library
    # --------------------------------------------------------------------------

    def _parse_fast(
        self, file_path: Path, doc_id: str
    ) -> tuple[list[RawBlock], list[int]] | None:
        """The text, read by the C library, and the pages with tables.

        Returns None when the fast path is not available at all - the
        caller then uses the slow path for everything.
        """
        try:
            import pymupdf
        except Exception as e:  # noqa: BLE001 - the fallback is the point
            logger.info(f"PyMuPDF is not available, using the slow path: {e!r}")
            return None

        blocks: list[RawBlock] = []
        table_pages: list[int] = []
        try:
            with pymupdf.open(file_path) as document:
                total_pages = document.page_count
                for page_number, page in enumerate(document, start=1):
                    text = page.get_text("text") or ""

                    if text.strip():
                        blocks.append(
                            RawBlock(
                                block_id=f"{doc_id}_p{page_number}_text",
                                doc_id=doc_id,
                                content=text.strip(),
                                type="text",
                                meta={
                                    "page": page_number,
                                    "page_total": total_pages,
                                    "is_scanned": False,
                                    "anchor": make_anchor("page", page=page_number),
                                    "parser": "pymupdf",
                                },
                            )
                        )

                    if self._has_ruled_table(page):
                        # The page has the ruling lines of a table.
                        # The slow path reads it - PyMuPDF's own table
                        # finder is far too slow to run on every page.
                        table_pages.append(page_number)
        except Exception as e:  # noqa: BLE001 - the slow path is the fallback
            logger.info(f"The fast path could not read {file_path}: {e!r}")
            return None

        return blocks, table_pages

    def _has_ruled_table(self, page: Any) -> bool:
        """Whether a page has the ruling lines of a table.

        PyMuPDF can find the tables itself, but its table finder is
        orders of magnitude slower than reading the text - a build
        would drown in it. The drawings that make up a table are cheap
        to look at, so that is what decides: the page goes to the slow
        path, and pdfplumber reads the table.
        """
        try:
            drawings = page.get_drawings()
        except Exception:
            return False

        lines = 0
        for drawing in drawings or []:
            for item in drawing.get("items", []) or []:
                kind = item[0] if isinstance(item, (list, tuple)) else ""
                if kind in ("l", "re"):
                    lines += 1
        # A table needs at least a header row and two columns.
        return lines >= 6

    # --------------------------------------------------------------------------
    # the slow path - the safety net
    # --------------------------------------------------------------------------

    def _parse_slow(
        self, file_path: Path, doc_id: str, keep: list[RawBlock] | None = None
    ) -> list[RawBlock]:
        """The text and the tables, read by pdfplumber.

        ``keep`` holds what the fast path already read: the slow path
        then only adds what is missing, so a document is not read
        twice into two copies of itself.
        """
        import pdfplumber

        blocks: list[RawBlock] = []

        # The pages the fast path already read: their text is not read
        # a second time. The tables still are - the slow path is the one
        # that finds them.
        fast_pages: set[Any] = set()
        if keep is not None:
            fast_pages = {
                block.meta.get("page")
                for block in keep
                if block.type == "text"
            }

        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)

            for page_num, page in enumerate(pdf.pages, start=1):
                text = "" if page_num in fast_pages else (page.extract_text() or "")
                tables = page.extract_tables()

                if text.strip():
                    blocks.append(
                        RawBlock(
                            block_id=f"{doc_id}_p{page_num}_text",
                            doc_id=doc_id,
                            content=text.strip(),
                            type="text",
                            meta={
                                "page": page_num,
                                "page_total": total_pages,
                                "is_scanned": False,
                                "anchor": make_anchor("page", page=page_num),
                                "parser": "pdfplumber",
                            },
                        )
                    )

                for table_idx, table in enumerate(tables):
                    if not table or not any(
                        any(cell for cell in row) for row in table
                    ):
                        continue
                    blocks.append(
                        RawBlock(
                            block_id=f"{doc_id}_p{page_num}_t{table_idx}",
                            doc_id=doc_id,
                            content=self._table_to_markdown(table),
                            type="table",
                            meta={
                                "page": page_num,
                                "page_total": total_pages,
                                "table_index": table_idx,
                                "anchor": make_anchor("page", page=page_num),
                                "parser": "pdfplumber",
                            },
                        )
                    )

                if not text.strip() and not tables:
                    blocks.append(
                        RawBlock(
                            block_id=f"{doc_id}_p{page_num}_scanned",
                            doc_id=doc_id,
                            content="[Scanned page - no text extracted]",
                            type="text",
                            meta={
                                "page": page_num,
                                "page_total": total_pages,
                                "is_scanned": True,
                                "anchor": make_anchor("page", page=page_num),
                                "parser": "pdfplumber",
                            },
                        )
                    )

        if keep is None:
            return blocks

        # The fast path got the text; the slow path got the tables and
        # the pages the fast path could not read.
        merged = list(keep)
        seen = {block.block_id for block in merged}
        for block in blocks:
            if block.block_id in seen:
                # The fast path already has this one.
                continue
            if block.type == "table" or block.meta.get("page") not in fast_pages:
                merged.append(block)
                seen.add(block.block_id)
        return merged

    # --------------------------------------------------------------------------
    # the rule
    # --------------------------------------------------------------------------

    def _needs_slow_path(
        self, blocks: list[RawBlock], table_pages: list[int]
    ) -> bool:
        """Whether the slow path has to take a turn.

        It takes one when:

        - the fast path came back with nothing at all - a document
          MuPDF could not read is a document the slow path may still be
          able to read; or
        - a page has the ruling lines of a table, or its text looks
          like one. The tables are the slow path's job.
        """
        if not blocks:
            return True
        if not any(
            block.type == "text" and block.content.strip() for block in blocks
        ):
            return True
        if table_pages:
            return True
        return self._looks_like_table(blocks)

    def _looks_like_table(self, blocks: list[RawBlock]) -> bool:
        """Whether a page's text looks like a table.

        Two lines in a row with columns in them - separated by a pipe,
        a tab, or a run of spaces. It is a cheap guess, and a wrong
        one only costs a second pass over one document.
        """
        for block in blocks:
            if block.type != "text":
                continue
            lines = [line for line in block.content.splitlines() if line.strip()]
            columns = [
                line
                for line in lines
                if "|" in line or "\t" in line or _COLUMN_GAP.search(line)
            ]
            if len(columns) >= 2:
                return True
        return False

    # --------------------------------------------------------------------------
    # the shared helpers
    # --------------------------------------------------------------------------

    def _table_to_markdown(self, table: list[list[str | None]]) -> str:
        """Convert a table to Markdown."""
        if not table:
            return ""

        # Clean None → empty string
        cleaned = [
            [(str(cell).strip() if cell is not None else "") for cell in row]
            for row in table
        ]

        # First row as header
        if not cleaned:
            return ""
        header = cleaned[0]
        rows = cleaned[1:]

        # Build markdown
        lines = ["| " + " | ".join(header) + " |"]
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
        for row in rows:
            # Pad row to match header length
            padded = row + [""] * (len(header) - len(row))
            lines.append("| " + " | ".join(padded[: len(header)]) + " |")
        return "\n".join(lines)

    async def extract_metadata(self, file_path: Path) -> dict[str, Any]:
        """Extract PDF metadata (title, author, etc.)."""
        def _extract():
            try:
                import pymupdf

                with pymupdf.open(file_path) as document:
                    meta = document.metadata or {}
                    return {
                        "title": meta.get("title", ""),
                        "author": meta.get("author", ""),
                        "subject": meta.get("subject", ""),
                        "creator": meta.get("creator", ""),
                        "page_count": document.page_count,
                    }
            except Exception:
                pass

            try:
                import pdfplumber

                with pdfplumber.open(file_path) as pdf:
                    meta = pdf.metadata or {}
                    return {
                        "title": meta.get("Title", ""),
                        "author": meta.get("Author", ""),
                        "subject": meta.get("Subject", ""),
                        "creator": meta.get("Creator", ""),
                        "page_count": len(pdf.pages),
                    }
            except Exception as e:
                logger.warning(f"Failed to extract PDF metadata: {e}")
                return {}

        return await asyncio.to_thread(_extract)
