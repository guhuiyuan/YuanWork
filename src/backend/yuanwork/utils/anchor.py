"""One anchor structure for every kind of position in a document.

The position of a chunk used to live in three fixed columns - ``page``,
``slide_number`` and ``section``. Excel's sheet and row had nowhere to go,
and a Markdown heading path did not fit into a single string.

An anchor is a dict with a ``kind`` and the coordinates that kind defines::

    {"kind": "page", "page": 12}
    {"kind": "slide", "slide": 3}
    {"kind": "section", "section": "第三章 费用",
     "heading_path": ["第三章 费用", "3.1 住宿"]}
    {"kind": "sheet_row", "sheet": "进度表", "row": 7}
    {"kind": "heading_path", "heading_path": ["第一章", "1.1 概述"]}

The kind is the switch: a reader asks *what kind is this* and then reads
the coordinates that kind defines. A kind that is missing a coordinate
means the position could not be pinned down - and a piece of evidence
without a position cannot be shown to the user, let alone checked.
"""

from __future__ import annotations

import json
from typing import Any

#: The five kinds of position a document can have.
ANCHOR_KINDS = ("page", "slide", "section", "sheet_row", "heading_path")

#: The coordinates each kind defines.
ANCHOR_COORDS: dict[str, tuple[str, ...]] = {
    "page": ("page",),
    "slide": ("slide",),
    "section": ("section", "heading_path"),
    "sheet_row": ("sheet", "row"),
    "heading_path": ("heading_path",),
}


def make_anchor(kind: str, **coords: Any) -> dict[str, Any]:
    """Build an anchor of the given kind.

    The coordinates the kind defines are kept; extra coordinates are kept
    too, because a few formats carry a finer position than their kind
    names (an image knows the line inside it).
    """
    if kind not in ANCHOR_KINDS:
        raise ValueError(
            f"Unknown anchor kind: {kind!r} (expected one of {', '.join(ANCHOR_KINDS)})"
        )

    anchor: dict[str, Any] = {"kind": kind}
    for key in ANCHOR_COORDS[kind]:
        value = coords.get(key)
        if value is not None:
            anchor[key] = value
    for key, value in coords.items():
        if key not in anchor and value is not None:
            anchor[key] = value
    return anchor


def anchor_kind(anchor: dict[str, Any] | None) -> str | None:
    """The kind of an anchor, if it has one."""
    if not anchor:
        return None
    kind = anchor.get("kind")
    return kind if kind in ANCHOR_KINDS else None


def anchor_of(chunk: dict[str, Any]) -> dict[str, Any] | None:
    """The anchor of a chunk, whatever shape the chunk has.

    Three sources, in order:

    1. the ``anchor`` the parser put in the chunk's metadata
    2. the ``anchor`` column of an indexed row (a JSON string)
    3. the three legacy columns - a row indexed before the anchor existed
       still has to say where it came from
    """
    meta = chunk.get("meta")
    meta = meta if isinstance(meta, dict) else {}

    raw = meta.get("anchor")
    if raw is None:
        raw = chunk.get("anchor")

    if isinstance(raw, dict):
        if anchor_kind(raw):
            return raw
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict) and anchor_kind(parsed):
            return parsed

    # Legacy rows: the position lived in three fixed columns. The nested
    # metadata wins over the flat column - it is the richer shape. A
    # slide wins over a page: a chunk is from one or the other, and the
    # old reader preferred the slide.
    slide = meta.get("slide_number")
    if slide is None:
        slide = chunk.get("slide_number")
    if slide is not None:
        return make_anchor("slide", slide=slide)

    page = meta.get("page")
    if page is None:
        page = chunk.get("page")
    if page is not None:
        return make_anchor("page", page=page)

    section = meta.get("section") or meta.get("heading") or chunk.get("section")
    if section:
        return make_anchor("section", section=section)
    return None


def has_anchor(chunk: dict[str, Any]) -> bool:
    """Whether the chunk can be pinned to a position in its document.

    Evidence without a position cannot be shown to the user, so it cannot
    be used to answer either.
    """
    anchor = anchor_of(chunk)
    if anchor is None:
        return False
    kind = anchor.get("kind")
    for key in ANCHOR_COORDS.get(kind, ()):
        if anchor.get(key) is not None:
            return True
    return False


def anchor_location(anchor: dict[str, Any] | None) -> str:
    """A human-readable position, e.g. ``第 12 页``.

    Returns an empty string when the anchor does not pin down a position.
    """
    if not anchor:
        return ""

    kind = anchor.get("kind")

    if kind == "page":
        if anchor.get("line") is not None:
            return f"第 {anchor['line']} 行"
        if anchor.get("page") is not None:
            return f"第 {anchor['page']} 页"
        return ""

    if kind == "slide":
        return f"Slide {anchor['slide']}" if anchor.get("slide") is not None else ""

    if kind == "section":
        if anchor.get("section"):
            return str(anchor["section"])
        path = anchor.get("heading_path")
        if path:
            return " › ".join(str(p) for p in path)
        return ""

    if kind == "sheet_row":
        sheet = anchor.get("sheet")
        row = anchor.get("row")
        if sheet and row is not None:
            return f"{sheet} 第 {row} 行"
        if sheet:
            return str(sheet)
        if row is not None:
            return f"第 {row} 行"
        return ""

    if kind == "heading_path":
        path = anchor.get("heading_path")
        if path:
            return " › ".join(str(p) for p in path)
        return ""

    return ""
