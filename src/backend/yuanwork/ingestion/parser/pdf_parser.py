"""PDF parser using pdfplumber (text) + PyMuPDF (scanned fallback)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from yuanwork.ingestion.parser.base import BaseParser, RawBlock

logger = logging.getLogger(__name__)


class PDFParser(BaseParser):
    """Parse PDF documents, preserving page-level metadata."""

    SUPPORTED_EXTENSIONS = ("pdf",)

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Parse PDF into page-level blocks."""
        return await asyncio.to_thread(self._parse_sync, file_path, doc_id)

    def _parse_sync(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Synchronous PDF parsing (runs in thread)."""
        import pdfplumber

        blocks: list[RawBlock] = []

        with pdfplumber.open(file_path) as pdf:
            total_pages = len(pdf.pages)

            for page_num, page in enumerate(pdf.pages, start=1):
                # Extract text
                text = page.extract_text() or ""

                # Extract tables separately
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
                            },
                        )
                    )

                # Add tables as separate blocks
                for table_idx, table in enumerate(tables):
                    if not table or not any(any(cell for cell in row) for row in table):
                        continue
                    # Convert table to Markdown
                    table_md = self._table_to_markdown(table)
                    blocks.append(
                        RawBlock(
                            block_id=f"{doc_id}_p{page_num}_t{table_idx}",
                            doc_id=doc_id,
                            content=table_md,
                            type="table",
                            meta={
                                "page": page_num,
                                "page_total": total_pages,
                                "table_index": table_idx,
                            },
                        )
                    )

                # If page has very little text, flag as scanned
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
                            },
                        )
                    )

        return blocks

    def _table_to_markdown(self, table: list[list[str | None]]) -> str:
        """Convert a pdfplumber table to Markdown."""
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
