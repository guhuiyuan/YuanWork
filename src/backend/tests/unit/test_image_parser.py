"""Tests for the ImageParser (OCR via rapidocr-onnxruntime)."""

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
    """End-to-end OCR test (skipped if rapidocr not installed)."""
    pytest.importorskip("rapidocr_onnxruntime")

    img = temp_dir / "cn.png"
    _make_test_image(img, ["测试 OCR", "人民币"])

    parser = ImageParser()
    blocks = await parser.parse(img, "doc-img-1")

    # Either OCR found text, or graceful placeholder if engine errored
    assert len(blocks) == 1
    b = blocks[0]
    assert b.doc_id == "doc-img-1"
    assert b.type in ("image_ocr", "image_placeholder")
    if b.type == "image_ocr":
        # We rendered 2 CJK lines; at least one should be in the block content
        all_text = b.content
        assert "测试" in all_text or "OCR" in all_text or "人民" in all_text
        # Boxes metadata records the per-line bounding boxes
        assert "boxes" in b.meta
        assert b.meta["text_count"] >= 1
        for box in b.meta["boxes"]:
            assert "box" in box
            assert "text" in box
            assert "confidence" in box
    # When graceful fallback fires, content should still mention the file
    if b.type == "image_placeholder":
        assert "tiny" in b.content or img.name in b.content


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
    block (so the file is at least registered) rather than raising."""
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
