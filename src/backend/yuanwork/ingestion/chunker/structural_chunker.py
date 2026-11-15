"""Structural chunker - respects document structure.

For most formats, the parser already produces well-structured blocks.
This chunker handles edge cases:
- Blocks that are too long → split with overlap
- Blocks that are too short → merge with neighbors
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from yuanwork.ingestion.parser.base import RawBlock

logger = logging.getLogger(__name__)


class StructuralChunker:
    """Convert RawBlocks to final chunks with size management."""

    # Token limits (bge-m3 supports up to 8192 tokens but practical limit is lower)
    MAX_CHARS = 2000  # ~500 tokens
    MIN_CHARS = 50
    OVERLAP_CHARS = 100

    def chunk(self, blocks: list[RawBlock], doc_id: str) -> list[dict[str, Any]]:
        """Convert blocks to chunks, splitting oversized ones."""
        chunks: list[dict[str, Any]] = []
        chunk_idx = 0

        for block in blocks:
            text = block.content.strip()
            if not text:
                continue

            if len(text) <= self.MAX_CHARS:
                # Use as-is
                chunk_idx += 1
                chunks.append(self._make_chunk(block, text, chunk_idx))
            else:
                # Split with overlap
                sub_chunks = self._split_long_text(text)
                for sub_text in sub_chunks:
                    chunk_idx += 1
                    chunks.append(self._make_chunk(block, sub_text, chunk_idx))

        return chunks

    def _make_chunk(
        self, block: RawBlock, text: str, chunk_idx: int
    ) -> dict[str, Any]:
        """Create a chunk dict from a block and text."""
        # Generate chunk ID from doc_id + block_id + chunk_idx
        id_source = f"{block.doc_id}|{block.block_id}|{chunk_idx}|{text[:100]}"
        chunk_id = hashlib.sha256(id_source.encode()).hexdigest()[:16]

        return {
            "id": chunk_id,
            "doc_id": block.doc_id,
            "chunk_idx": chunk_idx,
            "text": text,
            "type": block.type,
            "meta": block.meta,
        }

    def _split_long_text(self, text: str) -> list[str]:
        """Split text into overlapping chunks."""
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + self.MAX_CHARS, len(text))
            # Try to break at sentence boundary
            if end < len(text):
                for sep in ["\n\n", "。", ".\n", "! ", "? ", "\n"]:
                    last_sep = text.rfind(sep, start, end)
                    if last_sep > start + self.MAX_CHARS // 2:
                        end = last_sep + len(sep)
                        break
            piece = text[start:end].strip()
            if piece:
                chunks.append(piece)
            if end >= len(text):
                break
            start = end - self.OVERLAP_CHARS

        return self._merge_short_chunks(chunks)

    def _merge_short_chunks(self, chunks: list[str]) -> list[str]:
        """Fold a chunk that is too short into its neighbour.

        Dropping it would lose content: the tail of a document is often
        a short sentence, and a citation has to be able to point at it.
        A short chunk is therefore merged into the chunk before it, or -
        when that one is full - into the chunk after it.
        """
        merged: list[str] = []

        for chunk in chunks:
            if not merged:
                merged.append(chunk)
                continue

            previous = merged[-1]

            if len(chunk) < self.MIN_CHARS:
                if len(previous) + 1 + len(chunk) <= self.MAX_CHARS:
                    merged[-1] = f"{previous} {chunk}"
                    continue
                # The previous chunk is full: keep this one on its own and
                # let the next chunk fold into it.
                merged.append(chunk)
                continue

            if len(previous) < self.MIN_CHARS and len(previous) + 1 + len(chunk) <= self.MAX_CHARS:
                merged[-1] = f"{previous} {chunk}"
                continue

            merged.append(chunk)

        return merged
