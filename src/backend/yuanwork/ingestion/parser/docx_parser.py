"""Word (.docx) parser using python-docx."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from yuanwork.ingestion.parser.base import BaseParser, RawBlock

logger = logging.getLogger(__name__)


class DocxParser(BaseParser):
    """Parse Word documents, preserving heading structure."""

    SUPPORTED_EXTENSIONS = ("docx", "doc")

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Parse .docx into heading-grouped blocks."""
        return await asyncio.to_thread(self._parse_sync, file_path, doc_id)

    def _parse_sync(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Synchronous DOCX parsing."""
        from docx import Document

        doc = Document(file_path)
        blocks: list[RawBlock] = []

        current_heading: str | None = None
        current_heading_level: int = 0
        current_paragraphs: list[str] = []
        current_block_id: str | None = None

        def flush():
            nonlocal current_heading, current_heading_level, current_paragraphs, current_block_id
            if current_paragraphs:
                content = "\n".join(current_paragraphs).strip()
                if content:
                    blocks.append(
                        RawBlock(
                            block_id=current_block_id or f"{doc_id}_h{current_heading_level}",
                            doc_id=doc_id,
                            content=content,
                            type="text",
                            meta={
                                "heading": current_heading,
                                "heading_level": current_heading_level,
                            },
                        )
                    )
            current_paragraphs = []
            current_block_id = None

        for para_idx, para in enumerate(doc.paragraphs):
            style_name = para.style.name if para.style else ""
            is_heading = style_name.startswith("Heading")

            text = para.text.strip()
            if not text:
                continue

            if is_heading:
                # Flush previous section
                flush()
                current_heading = text
                # Extract heading level (Heading 1, Heading 2, etc.)
                try:
                    current_heading_level = int(style_name.split()[-1])
                except (ValueError, IndexError):
                    current_heading_level = 1
                current_block_id = f"{doc_id}_p{para_idx}_h{current_heading_level}"
            else:
                current_paragraphs.append(text)

        # Flush last section
        flush()

        # Also extract tables
        for table_idx, table in enumerate(doc.tables):
            table_md = self._table_to_markdown(table)
            blocks.append(
                RawBlock(
                    block_id=f"{doc_id}_table{table_idx}",
                    doc_id=doc_id,
                    content=table_md,
                    type="table",
                    meta={"table_index": table_idx},
                )
            )

        return blocks

    def _table_to_markdown(self, table) -> str:
        """Convert a docx table to Markdown."""
        rows = []
        for row in table.rows:
            cells = []
            for cell in row.cells:
                cell_text = "\n".join(p.text for p in cell.paragraphs).strip()
                cells.append(cell_text.replace("\n", "<br>"))
            rows.append(cells)

        if not rows:
            return ""

        lines = []
        header = rows[0]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
        for row in rows[1:]:
            padded = row + [""] * (len(header) - len(row))
            lines.append("| " + " | ".join(padded[: len(header)]) + " |")
        return "\n".join(lines)

    async def extract_metadata(self, file_path: Path) -> dict[str, Any]:
        """Extract DOCX core properties."""
        def _extract():
            try:
                from docx import Document
                doc = Document(file_path)
                cp = doc.core_properties
                return {
                    "title": cp.title or "",
                    "author": cp.author or "",
                    "subject": cp.subject or "",
                    "paragraph_count": len(doc.paragraphs),
                }
            except Exception as e:
                logger.warning(f"Failed to extract DOCX metadata: {e}")
                return {}

        return await asyncio.to_thread(_extract)
