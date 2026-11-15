"""Image parser with OCR (rapidocr-onnxruntime as the default backend).

Stage 2 differentiator: lets users drop screenshots / scanned documents
into the KB and have the extracted text indexed + searchable.

Chunking strategy (Stage 3 — OCR short-text fix):
  For each image we emit MULTIPLE blocks, not one:
    1. One `image_ocr` block per detected text line, content prefixed with
       `[OCR] ` for semantic anchoring. This addresses two problems:
         a) bge-m3 dense vectors of multi-field OCR strings dilute into
            a single "average" vector (see CSDN bge-m3 case study).
         b) The confidence scorer hard-refuses on `len(results) < 2`, so
            a single-block image always loses the chat — per-line blocks
            give us 6+ chunks per image and the scorer can do its job.
    2. One `image_ocr_full` block carrying the joined OCR text. This is
       the "context summary" anchor for queries that need the whole
       document in one hit (e.g. "总结这个截图").
    3. If OCR engine missing: a single `image_placeholder` block (with
       metadata only) so the file is at least registered in the index.
       Also: a per-line `image_ocr` block would be noise without text,
       so we don't emit it in this case.

The parser is **graceful** — if rapidocr-onnxruntime isn't installed, the
file is still registered as a placeholder block carrying only metadata
(width/height/format/EXIF). The chat pipeline can then surface a
"⚠️ image without OCR" hint instead of silently dropping the file.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from yuanwork.ingestion.parser.base import BaseParser, RawBlock
from yuanwork.utils.anchor import make_anchor

logger = logging.getLogger(__name__)


# Module-level engine is intentionally lazy. rapidocr model load takes
# a few seconds; we cache it in the parser instance rather than globally
# so each test can build a fresh parser and unit tests stay isolated.


class ImageParser(BaseParser):
    """Parse image files (png, jpg, bmp, tiff, webp) via OCR.

    Requires rapidocr-onnxruntime (preferred — pure ONNX, works on CPU
    without PaddlePaddle). Falls back to a metadata-only block if the
    engine isn't available.
    """

    SUPPORTED_EXTENSIONS = ("png", "jpg", "jpeg", "bmp", "tiff", "tif", "webp")

    def __init__(self) -> None:
        self._engine: Any = None
        self._engine_load_attempted = False
        self._engine_load_error: str | None = None

    def _get_engine(self) -> Any:
        """Lazily load the rapidocr engine (cached on first use)."""
        if self._engine is not None or self._engine_load_attempted:
            return self._engine
        self._engine_load_attempted = True
        try:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
            logger.info("rapidocr-onnxruntime engine loaded")
        except Exception as e:
            self._engine_load_error = repr(e)
            logger.warning(
                f"rapidocr-onnxruntime not available, image OCR disabled: {e!r}"
            )
        return self._engine

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Parse an image file via OCR.

        Returns:
            - N+1 blocks when OCR succeeds: N `image_ocr` (one per line)
              + 1 `image_ocr_full` (joined text)
            - 1 `image_placeholder` block if OCR engine missing
            - [] if the file can't be opened
        """
        meta = await self.extract_metadata(file_path)
        if "error" in meta:
            return []

        engine = await asyncio.to_thread(self._get_engine)
        if engine is None:
            return [
                RawBlock(
                    block_id=f"{doc_id}_image_placeholder",
                    doc_id=doc_id,
                    content=(
                        f"[图片: {file_path.name}]\n"
                        f"宽×高: {meta.get('width')}×{meta.get('height')}\n"
                        f"格式: {meta.get('format')}\n"
                        f"（OCR 不可用：{self._engine_load_error or 'engine not loaded'}）"
                    ),
                    type="image_placeholder",
                    meta=meta,
                )
            ]

        try:
            result = await asyncio.to_thread(self._run_ocr, file_path)
        except Exception as e:
            logger.warning(f"OCR failed for {file_path}: {e!r}")
            meta["ocr_error"] = repr(e)
            return [
                RawBlock(
                    block_id=f"{doc_id}_image_ocr_error",
                    doc_id=doc_id,
                    content=(
                        f"[图片: {file_path.name}] OCR 失败: {e!r}\n"
                        f"宽×高: {meta.get('width')}×{meta.get('height')}"
                    ),
                    type="image_placeholder",
                    meta=meta,
                )
            ]

        detections, _timing = result
        # An image without a single recognisable line comes back with
        # nothing at all - not with an empty list. It is not an error.
        detections = detections or []
        lines: list[str] = []
        boxes: list[dict[str, Any]] = []
        for det in detections:
            box, text, conf = det
            # Filter empty / very-low-confidence lines. Below 0.30 is
            # almost always a stray shape; we don't want it in the KB.
            if not text or not text.strip():
                continue
            if float(conf) < 0.30:
                continue
            lines.append(text.strip())
            boxes.append({
                "box": [[float(x), float(y)] for x, y in box],
                "text": text.strip(),
                "confidence": float(conf),
            })

        if not lines:
            return [
                RawBlock(
                    block_id=f"{doc_id}_image_no_text",
                    doc_id=doc_id,
                    content=(
                        f"[图片: {file_path.name}] 未识别出文字\n"
                        f"宽×高: {meta.get('width')}×{meta.get('height')}"
                    ),
                    type="image_ocr",
                    meta={**meta, "text_count": 0, "boxes": []},
                )
            ]

        # Emit one block per line. The [OCR] prefix is a Docling-style
        # "contextualize" header so the embedding model has a few extra
        # tokens of context to anchor on (bge-m3 prefers ≥10 tokens of
        # context for short fields).
        blocks: list[RawBlock] = []
        for i, (line, box) in enumerate(zip(lines, boxes)):
            blocks.append(
                RawBlock(
                    block_id=f"{doc_id}_image_ocr_line_{i}",
                    doc_id=doc_id,
                    content=f"[OCR] {line}",
                    type="image_ocr",
                    meta={
                        "line_index": i,
                        "confidence": box["confidence"],
                        "box": box["box"],
                        "file_name": file_path.name,
                        # An image has no pages, but it has lines: the OCR
                        # line is the position inside the picture.
                        "anchor": make_anchor("page", page=1, line=i + 1),
                    },
                )
            )

        # Plus one full-text block as the "context anchor" for queries
        # that need the whole document in one hit. The [OCR全文] prefix
        # marks it as the joined view (so retrieval can distinguish it
        # from per-line hits if we ever add a re-ranker that cares).
        joined = "\n".join(lines)
        blocks.append(
            RawBlock(
                block_id=f"{doc_id}_image_ocr_full",
                doc_id=doc_id,
                content=f"[OCR全文] {joined}",
                type="image_ocr_full",
                meta={
                    "line_count": len(lines),
                    "file_name": file_path.name,
                    "boxes": boxes,
                },
            )
        )

        return blocks

    def _run_ocr(self, file_path: Path) -> tuple:
        """Run OCR synchronously. rapidocr returns (detections, timing)."""
        return self._engine(str(file_path))

    async def extract_metadata(self, file_path: Path) -> dict[str, Any]:
        """Extract image dimensions / format / EXIF."""
        def _extract() -> dict[str, Any]:
            try:
                from PIL import Image
            except ImportError:
                return {"error": "Pillow not installed"}

            try:
                with Image.open(file_path) as img:
                    meta: dict[str, Any] = {
                        "width": img.width,
                        "height": img.height,
                        "format": img.format,
                        "mode": img.mode,
                    }
                    # EXIF (jpeg only typically)
                    exif = getattr(img, "_getexif", lambda: None)()
                    if exif:
                        # Tag 270 = ImageDescription, 305 = Software, etc.
                        meta["exif"] = {str(k): str(v)[:200] for k, v in exif.items()}
                    return meta
            except Exception as e:
                return {"error": repr(e)}

        return await asyncio.to_thread(_extract)
