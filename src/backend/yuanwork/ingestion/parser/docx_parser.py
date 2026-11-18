"""Word (.docx) parser using python-docx."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from yuanwork.ingestion.parser.base import BaseParser, RawBlock
from yuanwork.utils.anchor import make_anchor

logger = logging.getLogger(__name__)


class DocxParser(BaseParser):
    """Parse Word documents, preserving heading structure."""

    SUPPORTED_EXTENSIONS = ("docx", "doc")

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Parse .docx into heading-grouped blocks."""
        blocks, images = await asyncio.to_thread(self._parse_sync, file_path, doc_id)
        if images:
            blocks.extend(await self._image_blocks(doc_id, images, blocks))
        return blocks

    def _parse_sync(
        self, file_path: Path, doc_id: str
    ) -> tuple[list[RawBlock], list[tuple[str, bytes, list[str]]]]:
        """Synchronous DOCX parsing.

        Returns the blocks and the pictures inside the document - the
        pictures are read after this, because OCR is not something to
        do inside a thread.
        """
        from docx import Document

        doc = Document(file_path)
        blocks: list[RawBlock] = []

        current_heading: str | None = None
        current_heading_level: int = 0
        current_paragraphs: list[str] = []
        current_block_id: str | None = None
        # The headings above the current one, so a block can say which
        # section it sits in and how deep that section is nested.
        heading_stack: list[tuple[int, str]] = []

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
                                "heading_path": [t for _, t in heading_stack],
                                "anchor": make_anchor(
                                    "section",
                                    section=current_heading,
                                    heading_path=[t for _, t in heading_stack],
                                ),
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
                # A new heading closes the sections nested under the old one.
                while heading_stack and heading_stack[-1][0] >= current_heading_level:
                    heading_stack.pop()
                heading_stack.append((current_heading_level, text))
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
                    meta={
                        "table_index": table_idx,
                        "anchor": make_anchor(
                            "section",
                            section=current_heading,
                            heading_path=[t for _, t in heading_stack],
                        ),
                    },
                )
            )

        # The text that lives in text boxes.
        #
        # Word templates often put the whole document in floating text
        # boxes: the body paragraphs are empty spacers and the visible
        # text is inside `<w:txbxContent>`. python-docx reads neither -
        # `paragraph.text` only sees the direct text of a paragraph, and
        # it does not descend into a drawing. The result was **0 blocks**
        # for documents with plenty of text in them, and the build
        # dropped those files as "no content" - silently.
        textboxes = self._textbox_text(doc)
        for box_idx, text in enumerate(textboxes):
            blocks.append(
                RawBlock(
                    block_id=f"{doc_id}_txbx{box_idx}",
                    doc_id=doc_id,
                    content=text,
                    type="text",
                    meta={
                        "heading": None,
                        "heading_level": 0,
                        "heading_path": [],
                        "in_textbox": True,
                        "anchor": make_anchor(
                            "section",
                            section=None,
                            heading_path=[],
                            textbox_index=box_idx,
                        ),
                    },
                )
            )

        return blocks, self._images(doc)

    async def _image_blocks(
        self,
        doc_id: str,
        images: list[tuple[str, bytes, list[str]]],
        text_blocks: list[RawBlock],
    ) -> list[RawBlock]:
        """The text in the pictures inside the document.

        A scanned document is a picture with a file name: the text is in
        the image, not in the paragraphs. When the document has no text
        of its own, the pictures are all there is - and if the engine is
        not there, the file has to say so instead of being dropped as
        "no content" (AC-E-004).

        A document that *does* have text keeps its pictures quiet when
        they hold no text: a logo is not a finding.
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

        for name, data, heading_path in images:
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
                            "heading": heading_path[-1] if heading_path else None,
                            "heading_level": 0,
                            "heading_path": heading_path,
                            "anchor": make_anchor(
                                "section",
                                section=heading_path[-1] if heading_path else None,
                                heading_path=heading_path,
                                image_index=index,
                            ),
                        },
                    )
                )
            index += 1

        if text_blocks or read_any:
            return blocks

        # The document has nothing but pictures, and no text came out of
        # them. Say why - a file the user cannot find is worse than one
        # that says it could not be read.
        reason = engine_error() or "图片里没有识别出文字"
        if skipped_small:
            reason += f"（{skipped_small} 张图太小，没有读）"
        return [
            RawBlock(
                block_id=f"{doc_id}_image_placeholder",
                doc_id=doc_id,
                content=(
                    f"[图片文档] 这份文档的正文是图片（{len(images)} 张），"
                    f"文字读不出来：{reason}"
                ),
                type="image_placeholder",
                meta={
                    "in_image": True,
                    "image_count": len(images),
                    "heading": None,
                    "heading_level": 0,
                    "heading_path": [],
                    "anchor": make_anchor("section", section=None, heading_path=[]),
                },
            )
        ]

    @staticmethod
    def _images(doc) -> list[tuple[str, bytes, list[str]]]:
        """The pictures inside the document, with the headings above them.

        Each entry is (name, the bytes, the heading path it sits under).
        The pictures are found in the order they appear, so the heading
        above a picture is the one the reader would call its section.
        """
        from docx.oxml.ns import qn
        from docx.text.paragraph import Paragraph

        heading_stack: list[tuple[int, str]] = []
        images: list[tuple[str, bytes, list[str]]] = []
        seen: set[str] = set()

        for child in doc.element.body.iterchildren():
            if child.tag != qn("w:p"):
                continue

            try:
                paragraph = Paragraph(child, doc)
                style_name = paragraph.style.name if paragraph.style else ""
            except Exception:  # noqa: BLE001 - an odd paragraph is not fatal
                style_name = ""

            if style_name.startswith("Heading"):
                try:
                    level = int(style_name.split()[-1])
                except (ValueError, IndexError):
                    level = 1
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, paragraph.text.strip()))

            path = [title for _, title in heading_stack]

            for blip in child.iter(qn("a:blip")):
                rel_id = blip.get(qn("r:embed"))
                if not rel_id:
                    continue
                try:
                    part = doc.part.rels[rel_id].target_part
                    name = str(part.partname).rsplit("/", 1)[-1]
                    blob = part.blob
                except Exception:  # noqa: BLE001 - a broken picture is skipped
                    continue
                if name in seen:
                    continue
                seen.add(name)
                images.append((name, blob, list(path)))

        return images

    @staticmethod
    def _textbox_text(doc) -> list[str]:
        """The text of the document's text boxes, in order.

        Word writes a text box twice - once in the modern markup and
        once in the fallback for old Word - so the same text shows up
        twice in the XML. Boxes with identical text are counted once.
        """
        from docx.oxml.ns import qn

        seen: set[str] = set()
        texts: list[str] = []
        for element in doc.element.body.iter(qn("w:txbxContent")):
            lines: list[str] = []
            for paragraph in element.iter(qn("w:p")):
                text = "".join(
                    node.text or "" for node in paragraph.iter(qn("w:t"))
                ).strip()
                if text:
                    lines.append(text)
            content = "\n".join(lines).strip()
            if not content or content in seen:
                continue
            seen.add(content)
            texts.append(content)
        return texts

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
