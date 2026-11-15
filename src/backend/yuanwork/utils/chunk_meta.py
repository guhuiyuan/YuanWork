"""Reading location metadata off a chunk, whichever shape it has.

A chunk travels through YuanWork in two shapes:

- **right after chunking** (ingestion): ``{"id", ..., "meta": {"anchor": {...}}}``
  - the parser's metadata is still nested under ``meta``.
- **after retrieval**: a flat LanceDB row ``{"id", ..., "anchor": "{...}"}``
  - the metadata columns were lifted to the top level.

Reading ``chunk["meta"]["page"]`` on a retrieved row silently yields
``None``, so the page number never reaches the citation and the UI can
only say *which file*, not *where in it*. Reading ``chunk["page"]`` on a
fresh chunk has the same problem in the other direction.

This module gives one way to ask for a metadata field that works on both.
The position itself lives in an *anchor* (see ``yuanwork.utils.anchor``);
this module flattens the anchor's coordinates back into the familiar
field names so the rest of the code does not have to care where a
position came from.
"""

from __future__ import annotations

from typing import Any

from yuanwork.utils.anchor import ANCHOR_COORDS, anchor_location, anchor_of

#: Metadata fields that say *where* in a document a chunk came from.
LOCATION_FIELDS = (
    "page",
    "slide_number",
    "section",
    "heading",
    "sheet",
    "row",
    "line",
)

#: How the anchor's coordinates map onto the flat field names.
_ANCHOR_FIELD_NAMES = {
    "page": "page",
    "slide": "slide_number",
    "section": "section",
    "sheet": "sheet",
    "row": "row",
    "line": "line",
}


def chunk_meta(chunk: dict[str, Any]) -> dict[str, Any]:
    """Return the chunk's metadata, merged from both shapes.

    The nested ``meta`` dict wins over the flat columns, because it is
    the richer one (it also carries ``page_total``, ``is_scanned``, ...).
    The anchor wins over both: it is the position of record, and the flat
    columns may be leftovers from before the anchor existed.
    """
    nested = chunk.get("meta")
    merged: dict[str, Any] = dict(nested) if isinstance(nested, dict) else {}
    for key in LOCATION_FIELDS:
        if merged.get(key) is None and chunk.get(key) is not None:
            merged[key] = chunk[key]

    anchor = anchor_of(chunk)
    if anchor:
        merged["anchor"] = anchor
        for coord in ANCHOR_COORDS.get(anchor.get("kind", ""), ()):
            if anchor.get(coord) is not None:
                field = _ANCHOR_FIELD_NAMES.get(coord, coord)
                merged[field] = anchor[coord]
        # A heading path is worth keeping whole, not just its last element.
        if anchor.get("heading_path"):
            merged["heading_path"] = anchor["heading_path"]

    return merged


def chunk_location(chunk: dict[str, Any]) -> str:
    """A human-readable location for a chunk, e.g. ``第 12 页``.

    Returns an empty string when the chunk has no location metadata.
    """
    location = anchor_location(anchor_of(chunk))
    if location:
        return location

    meta = chunk_meta(chunk)
    if meta.get("slide_number"):
        return f"Slide {meta['slide_number']}"
    if meta.get("page"):
        return f"第 {meta['page']} 页"
    return ""
