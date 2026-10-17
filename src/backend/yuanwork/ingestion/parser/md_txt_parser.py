"""Markdown and plain text parser."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from yuanwork.ingestion.parser.base import BaseParser, RawBlock

logger = logging.getLogger(__name__)


class MarkdownTxtParser(BaseParser):
    """Parse Markdown and plain text files."""

    SUPPORTED_EXTENSIONS = ("md", "markdown", "txt")

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Parse text/markdown into heading-grouped blocks."""
        return await asyncio.to_thread(self._parse_sync, file_path, doc_id)

    def _parse_sync(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Synchronous parsing."""
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = file_path.read_text(encoding="gbk", errors="ignore")

        suffix = file_path.suffix.lower()
        if suffix in (".md", ".markdown"):
            return self._parse_markdown(content, doc_id)
        else:
            return self._parse_plain_text(content, doc_id)

    def _parse_markdown(self, content: str, doc_id: str) -> list[RawBlock]:
        """Parse Markdown by heading sections."""
        blocks: list[RawBlock] = []
        lines = content.split("\n")

        current_heading: str | None = None
        current_level: int = 0
        current_content: list[str] = []
        section_idx = 0

        def flush():
            nonlocal current_heading, current_level, current_content, section_idx
            text = "\n".join(current_content).strip()
            if text:
                section_idx += 1
                blocks.append(
                    RawBlock(
                        block_id=f"{doc_id}_section{section_idx}",
                        doc_id=doc_id,
                        content=text,
                        type="text",
                        meta={
                            "heading": current_heading,
                            "heading_level": current_level,
                            "section_index": section_idx,
                        },
                    )
                )
            current_content = []

        for line in lines:
            heading_match = re.match(r"^(#{1,6})\s+(.+)$", line)
            if heading_match:
                flush()
                level = len(heading_match.group(1))
                current_heading = heading_match.group(2).strip()
                current_level = level
            else:
                current_content.append(line)

        flush()
        return blocks

    def _parse_plain_text(self, content: str, doc_id: str) -> list[RawBlock]:
        """Parse plain text into paragraph chunks."""
        blocks: list[RawBlock] = []
        # Split by double newlines (paragraph breaks)
        paragraphs = re.split(r"\n\s*\n", content)
        chunk_idx = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            chunk_idx += 1
            blocks.append(
                RawBlock(
                    block_id=f"{doc_id}_p{chunk_idx}",
                    doc_id=doc_id,
                    content=para,
                    type="text",
                    meta={"paragraph_index": chunk_idx},
                )
            )

        return blocks

    async def extract_metadata(self, file_path: Path) -> dict[str, Any]:
        """Extract basic file metadata."""
        try:
            stat = file_path.stat()
            return {
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            }
        except Exception:
            return {}
