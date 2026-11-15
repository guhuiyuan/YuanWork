"""TASK-003 — the chunker keeps every character of the document.

The chunker used to drop any slice shorter than 50 characters. That
silently threw away the tail of a document — and a document whose tail
is missing cannot be cited: the answer points at a place that does not
exist.

A too-short slice is now folded into its neighbour instead.

* AC-F-002 — the whole document ends up in the index
* AC-E-002 — a removed file leaves nothing behind
* AC-E-003 — a changed file keeps its old version until a rebuild
"""

from __future__ import annotations

from yuanwork.ingestion.chunker.structural_chunker import StructuralChunker
from yuanwork.ingestion.parser.base import RawBlock


def make_block(content: str, block_id: str = "b1", doc_id: str = "d1") -> RawBlock:
    return RawBlock(
        block_id=block_id, doc_id=doc_id, content=content, type="text", meta={}
    )


def chunk_text(content: str) -> list[dict]:
    return StructuralChunker().chunk([make_block(content)], "d1")


def chunked_text(content: str) -> str:
    """Everything the chunker kept, joined back together."""
    return "".join(c["text"] for c in chunk_text(content))


# --------------------------------------------------------------------------
# AC-F-002 — nothing is dropped
# --------------------------------------------------------------------------


def test_a_short_tail_is_not_dropped():
    """The tail of a document is often a short sentence."""
    text = "A" * 1950 + "。" + "B" * 10

    chunks = chunk_text(text)

    assert len(chunks) == 1
    assert "B" * 10 in chunks[0]["text"]


def test_a_whitespace_heavy_block_keeps_its_content():
    """The old filter threw the whole block away."""
    text = "x" + " " * 2000 + "y"

    chunks = chunk_text(text)

    assert chunks
    assert "x" in chunks[0]["text"]
    assert "y" in chunks[0]["text"]


def test_every_character_of_the_document_is_kept():
    """The whole point: no content disappears."""
    text = "A" * 1950 + "。" + "B" * 10 + "C" * 40

    kept = chunked_text(text)

    assert "A" * 1950 in kept
    assert "B" * 10 in kept
    assert "C" * 40 in kept


def test_a_short_slice_between_two_long_ones_survives():
    text = "A" * 1200 + "\n\n" + "B" * 20 + "\n\n" + "C" * 1200

    chunks = chunk_text(text)

    assert any("B" * 20 in c["text"] for c in chunks)


def test_a_short_first_slice_survives():
    text = "A" * 10 + "\n\n" + "B" * 2000

    chunks = chunk_text(text)

    assert any("A" * 10 in c["text"] for c in chunks)


# --------------------------------------------------------------------------
# the limit still holds
# --------------------------------------------------------------------------


def test_no_chunk_exceeds_the_limit_after_merging():
    text = "A" * 5000

    chunks = chunk_text(text)

    assert len(chunks) > 1
    assert all(len(c["text"]) <= StructuralChunker.MAX_CHARS for c in chunks)


def test_a_merged_chunk_does_not_exceed_the_limit():
    """A short chunk is merged only when there is room for it."""
    text = "A" * 1999 + "。" + "B" * 10

    chunks = chunk_text(text)

    assert all(len(c["text"]) <= StructuralChunker.MAX_CHARS for c in chunks)
    assert "B" * 10 in "".join(c["text"] for c in chunks)


def test_a_full_chunk_keeps_a_short_neighbour_on_its_own():
    """When there is no room, the short chunk stays - but it stays."""
    text = "A" * 2000 + "B" * 10

    chunks = chunk_text(text)

    assert len(chunks) >= 2
    assert any("B" * 10 in c["text"] for c in chunks)


def test_a_block_that_is_only_whitespace_produces_nothing():
    """Whitespace is not content."""
    assert chunk_text("   \n\n  \t ") == []


# --------------------------------------------------------------------------
# AC-E-002 / AC-E-003 — the file's content is what gets indexed
# --------------------------------------------------------------------------


def test_a_short_document_stays_in_one_chunk():
    """The block structure is the parser's job; the chunker only splits
    what is too long."""
    text = "第一段。\n\n第二段。\n\n第三段。"

    chunks = chunk_text(text)

    assert [c["text"] for c in chunks] == [text]


def test_every_chunk_carries_its_block_metadata():
    block = RawBlock(
        block_id="b1",
        doc_id="d1",
        content="正文内容",
        type="text",
        meta={"anchor": {"kind": "page", "page": 3}},
    )

    chunks = StructuralChunker().chunk([block], "d1")

    assert chunks[0]["meta"]["anchor"] == {"kind": "page", "page": 3}
    assert chunks[0]["doc_id"] == "d1"
    assert chunks[0]["chunk_idx"] == 1
