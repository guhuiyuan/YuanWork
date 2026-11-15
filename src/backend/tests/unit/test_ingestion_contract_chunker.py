"""TASK-004 — the structural chunker's splitting rules.

The chunker is where a document's text gets cut, and the rules it
follows are unusual enough to be worth pinning:

* AC-F-007 — the three tuning parameters
* AC-F-008 — separator priority, the "past the midpoint" condition, and
  the fact that no separator is even looked for when the window already
  reaches the end of the text
* AC-F-009 — the next slice starts 100 characters back
* AC-P-001 — no slice is longer than 2000 characters
* AC-E-006 — a slice shorter than 50 characters is folded into its
  neighbour rather than dropped, so nothing is lost
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


# --------------------------------------------------------------------------
# AC-F-007 — the parameters
# --------------------------------------------------------------------------


def test_parameters_are_the_documented_ones():
    assert StructuralChunker.MAX_CHARS == 2000
    assert StructuralChunker.MIN_CHARS == 50
    assert StructuralChunker.OVERLAP_CHARS == 100


# --------------------------------------------------------------------------
# AC-P-001 — the upper bound
# --------------------------------------------------------------------------


def test_short_block_is_not_split():
    chunks = chunk_text("短文本内容。")
    assert len(chunks) == 1
    assert chunks[0]["text"] == "短文本内容。"


def test_block_exactly_at_the_limit_is_not_split():
    text = "A" * 2000
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert len(chunks[0]["text"]) == 2000


def test_no_slice_exceeds_the_limit():
    chunks = chunk_text("B" * 5000)
    assert len(chunks) > 1
    assert all(len(c["text"]) <= 2000 for c in chunks)


def test_whitespace_only_blocks_are_skipped():
    assert chunk_text("   \n\n  \t ") == []


# --------------------------------------------------------------------------
# AC-F-008 — where the cut happens
# --------------------------------------------------------------------------


def test_paragraph_break_wins_over_a_sentence_stop():
    text = "A" * 1200 + "\n\n" + "B" * 500 + "。" + "C" * 300
    chunks = chunk_text(text)
    assert len(chunks) == 2
    # the first slice ends at the paragraph break, not at the later stop
    assert chunks[0]["text"].endswith("A" * 10)
    assert "B" not in chunks[0]["text"]


def test_sentence_stop_is_used_when_no_paragraph_break_qualifies():
    text = "A" * 1200 + "。" + "B" * 900
    chunks = chunk_text(text)
    assert len(chunks) == 2
    assert chunks[0]["text"].endswith("。")


def test_a_separator_before_the_midpoint_is_ignored():
    """A separator that lands in the first half does not trigger a cut."""
    text = "A" * 300 + "。" + "B" * 2000
    chunks = chunk_text(text)
    assert len(chunks) == 2
    # hard cut at the limit instead
    assert len(chunks[0]["text"]) == 2000


def test_no_separator_is_looked_for_when_the_window_reaches_the_end():
    """A separator past the limit is ignored because the window already
    covers the rest of the text."""
    text = "A" * 1200 + "。" + "B" * 100
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert len(chunks[0]["text"]) == len(text)


def test_hard_cut_when_no_separator_qualifies():
    text = "C" * 2500
    chunks = chunk_text(text)
    # the second slice starts 100 characters back, so it spans 600 source
    # characters even though only 500 are new
    assert [len(c["text"]) for c in chunks] == [2000, 600]


# --------------------------------------------------------------------------
# AC-F-009 — the overlap
# --------------------------------------------------------------------------


def test_next_slice_starts_100_characters_back():
    text = "A" * 1200 + "\n\n" + "B" * 500 + "。" + "C" * 300
    chunks = chunk_text(text)
    # first slice ends at index 1202 (the paragraph break inclusive)
    # the second therefore starts at 1202 - 100 = 1102
    assert len(chunks[0]["text"]) == 1200
    assert len(chunks[1]["text"]) == 901
    assert chunks[1]["text"].startswith("A" * 10)


def test_overlap_applies_on_a_hard_cut_too():
    text = "D" * 2500
    chunks = chunk_text(text)
    assert len(chunks[0]["text"]) == 2000
    assert len(chunks[1]["text"]) == 600


# --------------------------------------------------------------------------
# AC-E-006 — the minimum length rule
#
# A slice that is too short is no longer dropped: it is folded into its
# neighbour, so the document keeps every character.
# --------------------------------------------------------------------------


def test_slices_shorter_than_50_characters_are_not_dropped():
    # a block that is mostly whitespace: both slices strip down to one
    # character, and both are kept
    chunks = chunk_text("x" + " " * 2000 + "y")
    assert chunks
    assert "x" in chunks[0]["text"]
    assert "y" in chunks[0]["text"]


def test_a_whitespace_heavy_block_keeps_its_content():
    text = "y" + " " * 2000 + "z"
    kept = chunk_text(text)
    assert kept
    assert "y" in kept[0]["text"]
    assert "z" in kept[0]["text"]


def test_a_long_enough_slice_survives():
    chunks = chunk_text("Q" * 60)
    assert len(chunks) == 1
    assert chunks[0]["text"] == "Q" * 60
