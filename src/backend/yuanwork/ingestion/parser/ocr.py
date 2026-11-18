"""OCR for the pictures that are inside a document.

A scanned document is a picture with a file name: the text is in the
image, not in the paragraphs. The image parser already reads the text
out of a picture - this is the same engine, used for the pictures that
are embedded in a .docx or a .pptx.

The proposal draws the line exactly here: the images are not
*understood*, only read for their text（「不做图片内容理解，图片只取其中
的文字」）. Reading the text is what this does.

The engine is loaded once per process and only when a picture actually
has to be read. A document without pictures never pays for it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Below this confidence a line is almost always a stray shape rather
#: than text - it does not belong in the index.
MIN_CONFIDENCE = 0.30

#: How many pictures in one document are read. Reading a picture takes
#: about a second and a half, and a document with a hundred of them
#: would hold the build on that one file.
MAX_IMAGES_PER_DOCUMENT = 20

#: Pictures smaller than this are logos and icons - their text is
#: noise, not content.
MIN_IMAGE_SIDE = 64

_ENGINE: Any = None
_ENGINE_TRIED = False
_ENGINE_ERROR: str | None = None


def get_engine() -> Any:
    """The OCR engine - loaded once, on first use.

    None when it is not there; `engine_error` says why.
    """
    global _ENGINE, _ENGINE_TRIED, _ENGINE_ERROR

    if _ENGINE is not None or _ENGINE_TRIED:
        return _ENGINE

    _ENGINE_TRIED = True
    try:
        from rapidocr_onnxruntime import RapidOCR

        _ENGINE = RapidOCR()
        logger.info("rapidocr-onnxruntime engine loaded")
    except Exception as e:  # noqa: BLE001 - a missing engine is not a failure
        _ENGINE_ERROR = repr(e)
        logger.warning(f"rapidocr-onnxruntime not available, OCR disabled: {e!r}")
    return _ENGINE


def engine_error() -> str | None:
    """Why the OCR engine is not there - None when it is."""
    return _ENGINE_ERROR


def reset_engine() -> None:
    """Forget the engine - the tests need a clean slate."""
    global _ENGINE, _ENGINE_TRIED, _ENGINE_ERROR

    _ENGINE = None
    _ENGINE_TRIED = False
    _ENGINE_ERROR = None


def image_is_worth_reading(data: bytes) -> bool:
    """Whether the picture is worth reading.

    Logos and icons are small - their text is noise, not content. A
    picture whose size cannot be read is worth reading anyway: the
    size is a filter, not a decision.
    """
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as img:
            width, height = img.size
    except Exception:  # noqa: BLE001 - an unreadable header is not a decision
        return True
    return min(width, height) >= MIN_IMAGE_SIDE


async def ocr_bytes(data: bytes) -> list[str]:
    """The text in an image, one entry per line.

    An empty list means there was no text to read - either the picture
    has none, or the engine is not there (`engine_error` says which).
    """
    engine = await asyncio.to_thread(get_engine)
    if engine is None:
        return []

    def _run() -> list[str]:
        result = engine(data)
        # rapidocr answers (detections, timing) - or (None, None) when
        # it found nothing at all.
        detections = result[0] if isinstance(result, tuple) else result
        detections = detections or []
        lines: list[str] = []
        for det in detections:
            try:
                _, text, score = det
            except (TypeError, ValueError):
                continue
            if not text or not str(text).strip():
                continue
            if float(score) < MIN_CONFIDENCE:
                continue
            lines.append(str(text).strip())
        return lines

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001 - one picture is not the whole file
        logger.warning(f"OCR failed: {e!r}")
        return []
