"""Tests for chunker."""

from __future__ import annotations

import pytest

from yuanwork.ingestion.chunker.structural_chunker import StructuralChunker
from yuanwork.ingestion.parser.base import RawBlock


def make_block(content: str, block_id: str = "block-1", doc_id: str = "doc-1") -> RawBlock:
    return RawBlock(
        block_id=block_id,
        doc_id=doc_id,
        content=content,
        type="text",
        meta={"page": 1},
    )


def test_short_block_unchanged():
    """Short blocks are not split."""
    chunker = StructuralChunker()
    block = make_block("短文本内容。")
    chunks = chunker.chunk([block], "doc-1")

    assert len(chunks) == 1
    assert chunks[0]["text"] == "短文本内容。"


def test_long_block_split():
    """Long blocks are split into overlapping chunks."""
    chunker = StructuralChunker()
    # The limit is the embedding model's read length in tokens
    # (ADR-046), not a number of characters.
    long_text = "这是一个测试句子。" * 300
    block = make_block(long_text)
    chunks = chunker.chunk([block], "doc-1")

    assert len(chunks) > 1
    # All chunks have content
    assert all(len(c["text"]) > 0 for c in chunks)
    # No chunk is longer than the model reads
    assert all(
        chunker.counter.count(c["text"]) <= chunker.max_tokens for c in chunks
    )


def test_chunk_ids_unique():
    """Chunk IDs are unique within and across blocks."""
    chunker = StructuralChunker()
    block1 = make_block("内容1", block_id="block-1")
    block2 = make_block("内容2", block_id="block-2")
    chunks = chunker.chunk([block1, block2], "doc-1")

    ids = [c["id"] for c in chunks]
    assert len(ids) == len(set(ids))  # All unique


def test_chunk_metadata_preserved():
    """Chunk metadata inherits from block."""
    chunker = StructuralChunker()
    block = make_block("测试内容", block_id="block-x")
    block.meta = {"page": 5, "slide_number": None}

    chunks = chunker.chunk([block], "doc-1")
    assert chunks[0]["meta"]["page"] == 5
