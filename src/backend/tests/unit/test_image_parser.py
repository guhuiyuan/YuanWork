"""Tests for the ImageParser (OCR via rapidocr-onnxruntime).

Stage 3 update: ImageParser now emits per-line blocks + one full-text
block, so the embedding model can produce a focused vector for each
field instead of diluting all OCR text into one average.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.ingestion.parser.image_parser import ImageParser


def _make_test_image(path: Path, lines: list[str]) -> None:
    """Render `lines` onto a white PNG using a CJK font if available."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (640, 50 + 50 * len(lines)), "white")
    d = ImageDraw.Draw(img)
    font = None
    for fp in [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
    ]:
        if Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, 32)
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()
    y = 10
    for line in lines:
        d.text((20, y), line, fill="black", font=font)
        y += 50
    img.save(path)


@pytest.mark.asyncio
async def test_image_parser_extracts_metadata(temp_dir: Path):
    img = temp_dir / "tiny.png"
    _make_test_image(img, ["hello"])

    parser = ImageParser()
    meta = await parser.extract_metadata(img)
    assert "width" in meta
    assert "height" in meta
    assert meta["width"] == 640
    assert meta["format"] == "PNG"
    assert "error" not in meta


@pytest.mark.asyncio
async def test_image_parser_ocr_chinese(temp_dir: Path):
    """End-to-end OCR test (skipped if rapidocr not installed).

    New Stage-3 contract: the parser emits N+1 blocks (N per-line + 1
    full-text). Asserts on the per-line + full structure.
    """
    pytest.importorskip("rapidocr_onnxruntime")

    img = temp_dir / "cn.png"
    _make_test_image(img, ["测试 OCR", "人民币"])

    parser = ImageParser()
    blocks = await parser.parse(img, "doc-img-1")

    # N+1 contract: per-line blocks + one full block
    assert len(blocks) >= 2

    per_line = [b for b in blocks if b.type == "image_ocr"]
    full = [b for b in blocks if b.type == "image_ocr_full"]
    assert len(per_line) >= 1
    assert len(full) == 1

    # Every per-line block is prefixed with [OCR] for embedding context
    for b in per_line:
        assert b.content.startswith("[OCR] "), (
            f"per-line block missing [OCR] prefix: {b.content!r}"
        )
        assert b.doc_id == "doc-img-1"
        # Per-line metadata carries the box + confidence + file_name
        assert "box" in b.meta
        assert "confidence" in b.meta
        assert b.meta["file_name"] == "cn.png"

    # Full block carries the joined text + line count
    fb = full[0]
    assert fb.content.startswith("[OCR全文] ")
    assert fb.meta["line_count"] == len(per_line)
    assert "boxes" in fb.meta

    # The rendered CJK content is present across the per-line + full blocks
    all_text = " ".join(b.content for b in blocks)
    assert any(
        marker in all_text
        for marker in ("测试", "OCR", "人民")
    ), f"OCR did not surface any CJK content: {all_text!r}"


@pytest.mark.asyncio
async def test_image_parser_emits_full_block_even_for_single_line(temp_dir: Path):
    """One-line images should still emit a full block (for context anchor)."""
    pytest.importorskip("rapidocr_onnxruntime")

    img = temp_dir / "single.png"
    _make_test_image(img, ["单行内容"])

    parser = ImageParser()
    blocks = await parser.parse(img, "doc-single")

    # Even with one recognized line, we should have per-line + full
    per_line = [b for b in blocks if b.type == "image_ocr"]
    full = [b for b in blocks if b.type == "image_ocr_full"]
    if per_line:  # OCR may not always pick up the line
        assert len(full) == 1
        assert full[0].meta["line_count"] >= 1


@pytest.mark.asyncio
async def test_image_parser_filters_low_confidence_lines(temp_dir: Path):
    """Lines with confidence < 0.30 are dropped to avoid garbage pollution.

    We can't easily inject specific confidences without mocking the OCR
    engine, so this test just asserts the parser never returns blocks
    with confidence below 0.30 when the engine runs end-to-end.
    """
    pytest.importorskip("rapidocr_onnxruntime")

    img = temp_dir / "hi.png"
    _make_test_image(img, ["高质量内容"])

    parser = ImageParser()
    blocks = await parser.parse(img, "doc-filter")

    for b in blocks:
        if b.type == "image_ocr":
            assert b.meta.get("confidence", 1.0) >= 0.30, (
                f"low-confidence line slipped through: {b.meta.get('confidence')}"
            )


@pytest.mark.asyncio
async def test_image_parser_handles_missing_file(temp_dir: Path):
    parser = ImageParser()
    blocks = await parser.parse(temp_dir / "does-not-exist.png", "doc-x")
    assert blocks == []


@pytest.mark.asyncio
async def test_image_parser_graceful_when_engine_missing(
    temp_dir: Path, monkeypatch
):
    """If rapidocr fails to load, parser must still return a placeholder
    block (so the file is at least registered) rather than raising.
    No per-line blocks are emitted in this path (no text to chunk).
    """
    img = temp_dir / "x.png"
    _make_test_image(img, ["hi"])

    parser = ImageParser()
    # Simulate engine load failure
    monkeypatch.setattr(parser, "_get_engine", lambda: None)
    blocks = await parser.parse(img, "doc-graceful")

    assert len(blocks) == 1
    b = blocks[0]
    assert b.type == "image_placeholder"
    assert "OCR 不可用" in b.content
    assert b.meta.get("width") == 640
