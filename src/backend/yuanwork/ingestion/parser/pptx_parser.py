"""PowerPoint (.pptx) parser using python-pptx.

CRITICAL: Preserves slide_number metadata - first-class citizen for citations.
Each slide produces one or more RawBlocks with slide_number preserved.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from yuanwork.ingestion.parser.base import BaseParser, RawBlock
from yuanwork.utils.anchor import make_anchor

logger = logging.getLogger(__name__)


class PptxParser(BaseParser):
    """Parse PPTX files, preserving slide-level structure."""

    SUPPORTED_EXTENSIONS = ("pptx", "ppt")

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Parse PPTX into slide-level blocks."""
        return await asyncio.to_thread(self._parse_sync, file_path, doc_id)

    def _parse_sync(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Synchronous PPTX parsing - core logic."""
        from pptx import Presentation
        from pptx.util import Emu

        prs = Presentation(file_path)
        blocks: list[RawBlock] = []
        total_slides = len(prs.slides)

        for slide_idx, slide in enumerate(prs.slides, start=1):
            # Get slide title
            slide_title = ""
            if slide.shapes.title:
                slide_title = slide.shapes.title.text or ""

            # Collect text shapes
            text_parts: list[str] = []
            table_blocks: list[str] = []
            notes_text = ""

            for shape in slide.shapes:
                if shape.has_text_frame:
                    text = shape.text_frame.text.strip()
                    if text and shape != slide.shapes.title:
                        text_parts.append(text)
                elif shape.has_table:
                    table_md = self._table_to_markdown(shape.table)
                    if table_md:
                        table_blocks.append(table_md)

            # Speaker notes
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    notes_text = notes

            # Build slide content
            content_parts = []
            if slide_title:
                content_parts.append(f"# {slide_title}")
            if text_parts:
                content_parts.append("\n".join(text_parts))
            if table_blocks:
                content_parts.append("\n\n".join(table_blocks))
            if notes_text:
                content_parts.append(f"\n[备注]\n{notes_text}")

            content = "\n\n".join(content_parts).strip()

            if content:
                blocks.append(
                    RawBlock(
                        block_id=f"{doc_id}_slide{slide_idx}",
                        doc_id=doc_id,
                        content=content,
                        type="text",
                        meta={
                            "slide_number": slide_idx,      # ★ FIRST-CLASS METADATA
                            "slide_total": total_slides,
                            "slide_title": slide_title,
                            "has_notes": bool(notes_text),
                            "has_tables": bool(table_blocks),
                            "anchor": make_anchor("slide", slide=slide_idx),
                        },
                    )
                )

            # Add tables as separate blocks for better retrieval
            for t_idx, table_md in enumerate(table_blocks):
                blocks.append(
                    RawBlock(
                        block_id=f"{doc_id}_slide{slide_idx}_t{t_idx}",
                        doc_id=doc_id,
                        content=table_md,
                        type="table",
                        meta={
                            "slide_number": slide_idx,
                            "slide_total": total_slides,
                            "slide_title": slide_title,
                            "table_index": t_idx,
                            "anchor": make_anchor("slide", slide=slide_idx),
                        },
                    )
                )

        return blocks

    def _table_to_markdown(self, table) -> str:
        """Convert a pptx table to Markdown."""
        rows = []
        for row in table.rows:
            cells = []
            for cell in row.cells:
                cells.append(cell.text.strip().replace("\n", "<br>"))
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
        """Extract PPTX metadata."""
        def _extract():
            try:
                from pptx import Presentation
                prs = Presentation(file_path)
                cp = prs.core_properties
                return {
                    "title": cp.title or "",
                    "author": cp.author or "",
                    "subject": cp.subject or "",
                    "slide_count": len(prs.slides),
                }
            except Exception as e:
                logger.warning(f"Failed to extract PPTX metadata: {e}")
                return {}

        return await asyncio.to_thread(_extract)
