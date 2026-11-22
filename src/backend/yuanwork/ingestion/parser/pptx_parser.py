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

    # The old binary `.ppt` is not here - python-pptx cannot open it and
    # this release does not convert it (proposal.md §2.2).
    SUPPORTED_EXTENSIONS = ("pptx",)

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Parse PPTX into slide-level blocks."""
        blocks, images = await asyncio.to_thread(self._parse_sync, file_path, doc_id)
        if images:
            blocks.extend(await self._image_blocks(doc_id, images, blocks))
        return blocks

    def _parse_sync(
        self, file_path: Path, doc_id: str
    ) -> tuple[list[RawBlock], list[tuple[int, str, bytes]]]:
        """Synchronous PPTX parsing - core logic.

        Returns the blocks and the pictures on the slides - the pictures
        are read after this, because OCR is not something to do inside a
        thread.
        """
        from pptx import Presentation
        from pptx.util import Emu

        prs = Presentation(file_path)
        blocks: list[RawBlock] = []
        total_slides = len(prs.slides)

        for slide_idx, slide in enumerate(prs.slides, start=1):
            # Get slide title.
            #
            # `slide.shapes.title` is not always a text shape. PowerPoint
            # puts embedded equation objects into a placeholder with
            # neither a type nor an index, and python-pptx hands that
            # object back as "the title" (index 0) - an object that has no
            # text at all. Asking it for `.text` raises, and the whole
            # file is lost. A slide without a text-frame title simply has
            # no title.
            slide_title = ""
            title_shape = slide.shapes.title
            if title_shape is not None and getattr(title_shape, "has_text_frame", False):
                slide_title = title_shape.text_frame.text.strip()

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

        return blocks, self._images(prs)

    async def _image_blocks(
        self,
        doc_id: str,
        images: list[tuple[int, str, bytes]],
        text_blocks: list[RawBlock],
    ) -> list[RawBlock]:
        """The text in the pictures on the slides.

        A slide that is a picture of a slide - a scan that came in as a
        .pptx - has its text in the image, not in a text frame. The
        picture keeps the slide it is on, so the citation can point at
        it (AC-E-004).
        """
        from yuanwork.ingestion.parser.ocr import (
            MAX_IMAGES_PER_DOCUMENT,
            engine_error,
            image_is_worth_reading,
            ocr_bytes,
        )

        blocks: list[RawBlock] = []
        read_any = False
        skipped_small = 0
        index = 0

        for slide_number, name, data in images:
            if index >= MAX_IMAGES_PER_DOCUMENT:
                break
            if not image_is_worth_reading(data):
                # A logo or an icon - its text is noise.
                skipped_small += 1
                continue
            lines = await ocr_bytes(data)
            if not lines:
                continue
            read_any = True
            for line_index, line in enumerate(lines):
                blocks.append(
                    RawBlock(
                        block_id=f"{doc_id}_img{index}_ocr{line_index}",
                        doc_id=doc_id,
                        content=f"[OCR] {line}",
                        type="image_ocr",
                        meta={
                            "in_image": True,
                            "image_index": index,
                            "image_name": name,
                            "line_index": line_index,
                            "slide_number": slide_number,
                            "anchor": make_anchor(
                                "slide", slide=slide_number, image_index=index
                            ),
                        },
                    )
                )
            index += 1

        if text_blocks or read_any:
            return blocks

        # The deck has nothing but pictures, and no text came out of
        # them. Say why.
        reason = engine_error() or "图片里没有识别出文字"
        if skipped_small:
            reason += f"（{skipped_small} 张图太小，没有读）"
        return [
            RawBlock(
                block_id=f"{doc_id}_image_placeholder",
                doc_id=doc_id,
                content=(
                    f"[图片幻灯片] 这份演示的正文是图片（{len(images)} 张），"
                    f"文字读不出来：{reason}"
                ),
                type="image_placeholder",
                meta={
                    "in_image": True,
                    "image_count": len(images),
                    "anchor": make_anchor("slide", slide=1),
                },
            )
        ]

    @staticmethod
    def _images(prs) -> list[tuple[int, str, bytes]]:
        """The pictures on the slides, with the slide they are on."""
        images: list[tuple[int, str, bytes]] = []
        for slide_idx, slide in enumerate(prs.slides, start=1):
            for shape in slide.shapes:
                try:
                    image = shape.image
                    blob = image.blob
                except Exception:  # noqa: BLE001 - a shape without a picture
                    continue
                name = getattr(image, "filename", "") or f"slide{slide_idx}"
                images.append((slide_idx, name, blob))
        return images

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
