"""Reading location metadata off a chunk, whichever shape it has.

A chunk travels through YuanWork in two shapes:

- **right after chunking** (ingestion): ``{"id", ..., "meta": {"page": 12}}``
  - the parser's metadata is still nested under ``meta``.
- **after retrieval**: a flat LanceDB row ``{"id", ..., "page": 12}``
  - the metadata columns were lifted to the top level.

Reading ``chunk["meta"]["page"]`` on a retrieved row silently yields
``None``, so the page number never reaches the citation and the UI can
only say *which file*, not *where in it*. Reading ``chunk["page"]`` on a
fresh chunk has the same problem in the other direction.

This module gives one way to ask for a metadata field that works on both.
"""

from __future__ import annotations

from typing import Any

#: Metadata fields that say *where* in a document a chunk came from.
LOCATION_FIELDS = ("page", "slide_number", "section", "heading")


def chunk_meta(chunk: dict[str, Any]) -> dict[str, Any]:
    """Return the chunk's metadata, merged from both shapes.

    The nested ``meta`` dict wins over the flat columns, because it is
    the richer one (it also carries ``page_total``, ``is_scanned``, ...).
    """
    nested = chunk.get("meta")
    merged: dict[str, Any] = dict(nested) if isinstance(nested, dict) else {}
    for key in LOCATION_FIELDS:
        if merged.get(key) is None and chunk.get(key) is not None:
            merged[key] = chunk[key]
    return merged


def chunk_location(chunk: dict[str, Any]) -> str:
    """A human-readable location for a chunk, e.g. ``第 12 页``.

    Returns an empty string when the chunk has no location metadata.
    """
    meta = chunk_meta(chunk)
    if meta.get("slide_number"):
        return f"Slide {meta['slide_number']}"
    if meta.get("page"):
        return f"第 {meta['page']} 页"
    return ""
