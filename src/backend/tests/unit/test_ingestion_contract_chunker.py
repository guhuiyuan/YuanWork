"""TASK-004 — the structural chunker's splitting rules.

The chunker is where a document's text gets cut, and the rules it
follows are unusual enough to be worth pinning:

* the tuning parameters
* separator priority, the "past the midpoint" condition, and the fact
  that no separator is even looked for when the window already reaches
  the end of the text
* the next piece starts 100 characters back
* no piece is longer than the limit
* a piece shorter than 50 characters is folded into its neighbour
  rather than dropped, so nothing is lost

**The limit is in tokens, not characters** (ADR-046): it is the
embedding model's read length, so that a piece is never longer than
what the model reads. The rules are pinned with a counter that says
"one character, one token" - the real tokenizer's numbers depend on
the text, and the rules are what is being pinned here.
"""

from __future__ import annotations

from yuanwork.ingestion.chunker.structural_chunker import StructuralChunker
from yuanwork.ingestion.chunker.token_counter import TokenCounter
from yuanwork.ingestion.parser.base import RawBlock

LIMIT = 510


class OneTokenPerChar:
    """One character, one token - so the rules can be pinned exactly."""

    def count(self, text: str) -> int:
        return len(text)

    def boundary(self, text: str, limit: int) -> int:
        return min(limit, len(text))


def make_block(content: str, block_id: str = "b1", doc_id: str = "d1") -> RawBlock:
    return RawBlock(
        block_id=block_id, doc_id=doc_id, content=content, type="text", meta={}
    )


def chunk_text(content: str) -> list[dict]:
    chunker = StructuralChunker(max_tokens=LIMIT, token_counter=OneTokenPerChar())
    return chunker.chunk([make_block(content)], "d1")


# --------------------------------------------------------------------------
# the parameters
# --------------------------------------------------------------------------


def test_parameters_are_the_documented_ones():
    assert StructuralChunker.MAX_TOKENS == 510
    assert StructuralChunker.MIN_CHARS == 50
    assert StructuralChunker.OVERLAP_CHARS == 100


def test_the_default_limit_is_the_models_read_length():
    """512 token is the model's read length; the content gets 510."""
    from yuanwork.ingestion.chunker.token_counter import content_token_limit

    assert content_token_limit(512) == 510
    assert StructuralChunker().max_tokens == 510


# --------------------------------------------------------------------------
# the upper bound
# --------------------------------------------------------------------------


def test_short_block_is_not_split():
    chunks = chunk_text("短文本内容。")
    assert len(chunks) == 1
    assert chunks[0]["text"] == "短文本内容。"


def test_block_exactly_at_the_limit_is_not_split():
    text = "A" * LIMIT
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert len(chunks[0]["text"]) == LIMIT


def test_no_slice_exceeds_the_limit():
    chunks = chunk_text("B" * 5000)
    assert len(chunks) > 1
    assert all(len(c["text"]) <= LIMIT for c in chunks)


def test_whitespace_only_blocks_are_skipped():
    assert chunk_text("   \n\n  \t ") == []


# --------------------------------------------------------------------------
# where the cut happens
# --------------------------------------------------------------------------


def test_paragraph_break_wins_over_a_sentence_stop():
    text = "A" * 300 + "\n\n" + "B" * 500 + "。" + "C" * 300
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    # the first slice ends at the paragraph break, not at the later stop
    assert chunks[0]["text"].endswith("A" * 10)
    assert "B" not in chunks[0]["text"]


def test_sentence_stop_is_used_when_no_paragraph_break_qualifies():
    text = "A" * 300 + "。" + "B" * 400
    chunks = chunk_text(text)
    assert len(chunks) >= 2
    assert chunks[0]["text"].endswith("。")


def test_a_separator_before_the_midpoint_is_ignored():
    """A separator that lands in the first half does not trigger a cut."""
    text = "A" * 100 + "。" + "B" * 1000
    chunks = chunk_text(text)
    # hard cut at the limit instead
    assert len(chunks[0]["text"]) == LIMIT
    assert not chunks[0]["text"].endswith("。")


def test_no_separator_is_looked_for_when_the_window_reaches_the_end():
    """A separator past the limit is ignored because the window already
    covers the rest of the text."""
    text = "A" * 300 + "。" + "B" * 100
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert len(chunks[0]["text"]) == len(text)


def test_hard_cut_when_no_separator_qualifies():
    text = "C" * 2500
    chunks = chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c["text"]) <= LIMIT for c in chunks)


# --------------------------------------------------------------------------
# the overlap
# --------------------------------------------------------------------------


def test_next_slice_starts_100_characters_back():
    # No separator anywhere, so the cut is a hard one - and the head of
    # the next piece is the tail of the one before it.
    text = "A" * 300 + "B" * 300
    chunks = chunk_text(text)
    assert chunks[1]["text"].startswith(chunks[0]["text"][-100:])


def test_overlap_applies_on_a_hard_cut_too():
    text = "D" * 2500
    chunks = chunk_text(text)
    assert len(chunks[0]["text"]) == LIMIT
    assert chunks[1]["text"].startswith(chunks[0]["text"][-100:])


# --------------------------------------------------------------------------
# the minimum length rule
#
# A piece that is too short is no longer dropped: it is folded into its
# neighbour, so the document keeps every character.
# --------------------------------------------------------------------------


def test_slices_shorter_than_50_characters_are_not_dropped():
    # a block that is mostly whitespace: both pieces strip down to one
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


# --------------------------------------------------------------------------
# the counter without a tokenizer
# --------------------------------------------------------------------------


def test_the_counter_without_a_model_uses_the_estimate():
    counter = TokenCounter(model_name=None)
    assert not counter.exact
    assert counter.count("采购管理办法") > 0


def test_the_estimated_boundary_is_where_the_estimate_says():
    counter = TokenCounter(model_name=None)
    text = "A" * 2000
    boundary = counter.boundary(text, 100)
    assert 0 < boundary < len(text)
    assert counter.count(text[:boundary]) <= 100
