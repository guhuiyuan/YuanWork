"""TASK-006 — the image parser's OCR contract.

The OCR engine is slow and unavailable in some environments, so these
tests drive it with a fake engine wherever the *rule* is under test, and
fall back to the real engine only for the shape assertions.

* AC-F-012 — one block per recognized line plus one full-text block
* AC-F-013 — lines below 0.30 confidence are dropped
* AC-E-002 — an unreadable image produces no blocks at all
* AC-E-012 — the EXIF orientation tag is not applied
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.ingestion.parser.image_parser import ImageParser
from unit.ingestion_fixtures import (
    make_corrupt_image_file,
    make_exif_image_file,
    make_image_file,
)


class FakeOCREngine:
    """Stands in for the rapidocr engine.

    ``detections`` is a list of ``(box, text, confidence)`` triples.
    """

    def __init__(self, detections):
        self.detections = detections

    def __call__(self, path):
        return (self.detections, {"elapsed": 1})


def box():
    return [[0, 0], [100, 0], [100, 20], [0, 20]]


# --------------------------------------------------------------------------
# AC-F-012 — per-line blocks plus one full-text block
# --------------------------------------------------------------------------


async def test_one_block_per_line_plus_one_full_block(temp_dir: Path):
    pytest.importorskip("PIL")
    path = make_image_file(temp_dir, "shot.png", ["第一行", "第二行", "第三行"])

    parser = ImageParser()
    parser._engine = FakeOCREngine(
        [
            (box(), "第一行", 0.95),
            (box(), "第二行", 0.90),
            (box(), "第三行", 0.88),
        ]
    )
    blocks = await parser.parse(path, "doc1")

    per_line = [b for b in blocks if b.type == "image_ocr"]
    full = [b for b in blocks if b.type == "image_ocr_full"]

    assert len(per_line) == 3
    assert len(full) == 1

    for block in per_line:
        assert block.content.startswith("[OCR] ")
        assert block.doc_id == "doc1"
        assert "box" in block.meta
        assert "confidence" in block.meta
        assert block.meta["file_name"] == "shot.png"

    assert full[0].content.startswith("[OCR全文] ")
    assert full[0].meta["line_count"] == 3
    assert "boxes" in full[0].meta


async def test_per_line_blocks_carry_their_index(temp_dir: Path):
    pytest.importorskip("PIL")
    path = make_image_file(temp_dir, "shot.png", ["a", "b"])
    parser = ImageParser()
    parser._engine = FakeOCREngine([(box(), "a", 0.9), (box(), "b", 0.9)])
    blocks = await parser.parse(path, "doc1")
    per_line = [b for b in blocks if b.type == "image_ocr"]
    assert [b.meta["line_index"] for b in per_line] == [0, 1]


# --------------------------------------------------------------------------
# AC-F-013 — the confidence filter
# --------------------------------------------------------------------------


async def test_low_confidence_lines_are_dropped(temp_dir: Path):
    pytest.importorskip("PIL")
    path = make_image_file(temp_dir, "shot.png", ["good", "bad"])

    parser = ImageParser()
    parser._engine = FakeOCREngine(
        [
            (box(), "保留的行", 0.95),
            (box(), "丢弃的行", 0.29),
        ]
    )
    blocks = await parser.parse(path, "doc1")

    per_line = [b for b in blocks if b.type == "image_ocr"]
    assert len(per_line) == 1
    assert "保留的行" in per_line[0].content
    assert all(b.meta["confidence"] >= 0.30 for b in per_line)


async def test_a_line_exactly_at_the_threshold_is_kept(temp_dir: Path):
    pytest.importorskip("PIL")
    path = make_image_file(temp_dir, "shot.png", ["edge"])
    parser = ImageParser()
    parser._engine = FakeOCREngine([(box(), "临界行", 0.30)])
    blocks = await parser.parse(path, "doc1")
    per_line = [b for b in blocks if b.type == "image_ocr"]
    assert len(per_line) == 1


async def test_empty_text_lines_are_dropped(temp_dir: Path):
    pytest.importorskip("PIL")
    path = make_image_file(temp_dir, "shot.png", ["x"])
    parser = ImageParser()
    parser._engine = FakeOCREngine(
        [(box(), "有效", 0.9), (box(), "   ", 0.9), (box(), "", 0.9)]
    )
    blocks = await parser.parse(path, "doc1")
    per_line = [b for b in blocks if b.type == "image_ocr"]
    assert len(per_line) == 1


async def test_an_image_without_text_still_yields_one_block(temp_dir: Path):
    pytest.importorskip("PIL")
    path = make_image_file(temp_dir, "shot.png", ["x"])
    parser = ImageParser()
    parser._engine = FakeOCREngine([])
    blocks = await parser.parse(path, "doc1")
    assert len(blocks) == 1
    assert blocks[0].type == "image_ocr"
    assert "未识别出文字" in blocks[0].content
    assert blocks[0].meta["text_count"] == 0


# --------------------------------------------------------------------------
# AC-E-002 — an unreadable image
# --------------------------------------------------------------------------


async def test_corrupt_image_yields_no_blocks(temp_dir: Path):
    pytest.importorskip("PIL")
    path = make_corrupt_image_file(temp_dir)
    blocks = await ImageParser().parse(path, "doc1")
    assert blocks == []


async def test_missing_image_yields_no_blocks(temp_dir: Path):
    blocks = await ImageParser().parse(temp_dir / "nope.png", "doc1")
    assert blocks == []


# --------------------------------------------------------------------------
# AC-E-012 — the EXIF orientation is not applied
# --------------------------------------------------------------------------


async def test_exif_orientation_is_not_applied_to_the_dimensions(temp_dir: Path):
    pytest.importorskip("PIL")
    path = make_exif_image_file(temp_dir)
    meta = await ImageParser().extract_metadata(path)
    assert "error" not in meta
    # the tag says "rotate 90", but the recorded size is the raw one
    assert (meta["width"], meta["height"]) == (200, 100)
    assert meta["exif"]


# --------------------------------------------------------------------------
# the placeholder path (engine unavailable)
# --------------------------------------------------------------------------


async def test_a_missing_engine_yields_a_placeholder_block(temp_dir: Path, monkeypatch):
    pytest.importorskip("PIL")
    path = make_image_file(temp_dir, "shot.png", ["x"])
    parser = ImageParser()
    monkeypatch.setattr(parser, "_get_engine", lambda: None)
    blocks = await parser.parse(path, "doc1")
    assert len(blocks) == 1
    assert blocks[0].type == "image_placeholder"
    assert "OCR 不可用" in blocks[0].content
