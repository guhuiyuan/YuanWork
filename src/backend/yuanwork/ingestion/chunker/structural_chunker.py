"""Structural chunker - respects document structure.

For most formats, the parser already produces well-structured blocks.
This chunker handles edge cases:
- Blocks that are too long → split with overlap
- Blocks that are too short → merge with neighbors

The limit is the **embedding model's read length** in **tokens**, not a
number of characters. The two used to disagree - the chunker cut at
2000 characters (a figure that only holds for English) while the model
read 512 tokens - and the difference was content nobody could find
again: a chunk whose tail the model never read is a chunk whose tail
nobody can retrieve (ADR-046).

How long a text is, and where the limit falls, is the counter's
business (``token_counter``). The rules below - where to cut, how much
to overlap, when to fold a short piece into its neighbour - are the
chunker's.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from yuanwork.ingestion.chunker.token_counter import TokenCounter
from yuanwork.ingestion.parser.base import RawBlock

logger = logging.getLogger(__name__)


class StructuralChunker:
    """Convert RawBlocks to final chunks with size management."""

    # How many tokens a chunk may have. It is the embedding model's read
    # length (minus the two special tokens the model adds), so that a
    # chunk is never longer than what the model reads - the same number
    # for both, from one setting (ADR-046).
    MAX_TOKENS = 510
    MIN_CHARS = 50

    # How much two neighbouring pieces overlap. It is in characters,
    # because the overlap is a little bit of text either way - the
    # point of it is that a fact on the border is in both pieces.
    OVERLAP_CHARS = 100

    # Where to cut, in order of preference: a paragraph break first, a
    # sentence stop second.
    SEPARATORS = ("\n\n", "。", ".\n", "! ", "? ", "\n")

    #: The rules of the cutting. It changes whenever they do - and the
    #: index compares it, so that what was cut with the old rules is
    #: cut again instead of being skipped as "already indexed"
    #: (ADR-046). The first version was "chars-2000"; the rules are
    #: now measured in tokens.
    RULES_VERSION = "token-v1"

    def __init__(
        self,
        max_tokens: int | None = None,
        token_counter: TokenCounter | None = None,
    ) -> None:
        self.max_tokens = max(1, int(max_tokens or self.MAX_TOKENS))
        self.counter = token_counter or TokenCounter()

    @property
    def version(self) -> str:
        """What the index remembers: the rules *and* the limit."""
        return f"{self.RULES_VERSION}-{self.max_tokens}"

    def chunk(self, blocks: list[RawBlock], doc_id: str) -> list[dict[str, Any]]:
        """Convert blocks to chunks, splitting oversized ones."""
        chunks: list[dict[str, Any]] = []
        chunk_idx = 0

        for block in blocks:
            text = block.content.strip()
            if not text:
                continue

            if self.counter.count(text) <= self.max_tokens:
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
        pieces: list[str] = []
        start = 0
        length = len(text)

        while start < length:
            window = text[start:]
            end = self.counter.boundary(window, self.max_tokens)
            if end < len(window):
                cut = self._separator_cut(window, end)
                if cut > 0:
                    end = cut
            piece = window[:end].strip()
            if piece:
                pieces.append(piece)
            if end >= len(window):
                break
            # The next piece starts a little back, so a fact that
            # straddles the cut is in both of them.
            start = start + max(1, end - self.OVERLAP_CHARS)

        return self._merge_short_chunks(self._enforce_limit(pieces))

    def _separator_cut(self, window: str, end: int) -> int:
        """Where to cut instead: at a separator, if one qualifies.

        The separators are tried in order of preference (a paragraph
        break before a sentence stop), and a separator only counts when
        it lands in the **second half** of the window - cutting at an
        early one would leave a tiny piece behind.
        """
        midpoint = end // 2
        for separator in self.SEPARATORS:
            found = window.rfind(separator, 0, end)
            if found > midpoint:
                return found + len(separator)
        return -1

    def _enforce_limit(self, pieces: list[str]) -> list[str]:
        """Cut again the pieces that came out over the limit.

        A piece is a substring, and measuring it again does not always
        give the same number - the boundaries land differently. A piece
        the model cannot read in full is a piece whose tail nobody can
        find, so it is cut again.
        """
        result: list[str] = []
        for piece in pieces:
            if not piece.strip():
                continue
            if self.counter.count(piece) <= self.max_tokens:
                result.append(piece)
                continue
            half = len(piece) // 2
            halves = [part for part in (piece[:half], piece[half:]) if part.strip()]
            if not halves:
                result.append(piece)
                continue
            result.extend(self._enforce_limit(halves))
        return result

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
                if self._fits(previous, chunk):
                    merged[-1] = f"{previous} {chunk}"
                    continue
                # The previous chunk is full: keep this one on its own and
                # let the next chunk fold into it.
                merged.append(chunk)
                continue

            if len(previous) < self.MIN_CHARS and self._fits(previous, chunk):
                merged[-1] = f"{previous} {chunk}"
                continue

            merged.append(chunk)

        return merged

    def _fits(self, first: str, second: str) -> bool:
        """Whether two chunks can be one chunk without going over the limit."""
        return self.counter.count(f"{first} {second}") <= self.max_tokens
