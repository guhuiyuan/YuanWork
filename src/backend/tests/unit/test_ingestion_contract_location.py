"""TASK-002 — the location metadata contract.

A chunk travels through the system in two shapes: nested right after
chunking, flat after retrieval. `chunk_meta` is the one function that
reads a location field from either shape — and it is what decides
whether the answer can say "第 12 页" or only "which file".

The position itself is an *anchor*; `chunk_meta` flattens the anchor's
coordinates back into the familiar field names, so the rest of the code
does not have to care where a position came from.

* AC-F-010 — the merged read and the field set it covers
* AC-F-003 — a section and a heading *are* positions now
"""

from __future__ import annotations

from yuanwork.utils.anchor import make_anchor
from yuanwork.utils.chunk_meta import LOCATION_FIELDS, chunk_location, chunk_meta


# --------------------------------------------------------------------------
# AC-F-010 — the merged read
# --------------------------------------------------------------------------


def test_location_field_set_covers_every_kind_of_position():
    assert LOCATION_FIELDS == (
        "page",
        "slide_number",
        "section",
        "heading",
        "sheet",
        "row",
        "line",
    )


def test_nested_shape_is_read():
    chunk = {"id": "c1", "meta": {"page": 12, "slide_number": None}}
    merged = chunk_meta(chunk)
    assert merged["page"] == 12


def test_flat_shape_is_read():
    chunk = {"id": "c1", "page": 12}
    merged = chunk_meta(chunk)
    assert merged["page"] == 12


def test_nested_shape_wins_over_the_flat_column():
    chunk = {"id": "c1", "meta": {"page": 12}, "page": 99}
    assert chunk_meta(chunk)["page"] == 12


def test_flat_column_fills_a_gap_in_the_nested_shape():
    chunk = {"id": "c1", "meta": {"page": None}, "page": 7}
    assert chunk_meta(chunk)["page"] == 7


def test_non_location_metadata_survives_the_merge():
    chunk = {"id": "c1", "meta": {"page": 3, "page_total": 20, "is_scanned": False}}
    merged = chunk_meta(chunk)
    assert merged["page_total"] == 20
    assert merged["is_scanned"] is False


def test_a_missing_meta_key_is_simply_absent():
    merged = chunk_meta({"id": "c1"})
    assert merged == {}


def test_a_non_dict_meta_is_ignored():
    merged = chunk_meta({"id": "c1", "meta": "not a dict", "page": 5})
    assert merged["page"] == 5


def test_a_chunk_without_any_location_reads_as_empty():
    assert chunk_meta({"id": "c1", "text": "..."}) == {}


# --------------------------------------------------------------------------
# the anchor is the position of record
# --------------------------------------------------------------------------


def test_the_anchor_wins_over_the_flat_columns():
    chunk = {
        "id": "c1",
        "meta": {"anchor": make_anchor("page", page=12)},
        "page": 99,
    }
    assert chunk_meta(chunk)["page"] == 12


def test_the_anchor_sheet_and_row_reach_the_flat_fields():
    chunk = {"id": "c1", "meta": {"anchor": make_anchor("sheet_row", sheet="台账", row=7)}}
    merged = chunk_meta(chunk)
    assert merged["sheet"] == "台账"
    assert merged["row"] == 7


# --------------------------------------------------------------------------
# the human readable form
# --------------------------------------------------------------------------


def test_slide_number_wins_over_page():
    chunk = {"meta": {"slide_number": 3, "page": 12}}
    assert chunk_location(chunk) == "Slide 3"


def test_page_is_rendered_in_chinese():
    chunk = {"meta": {"page": 12}}
    assert chunk_location(chunk) == "第 12 页"


def test_a_chunk_without_location_renders_as_empty():
    assert chunk_location({"meta": {}}) == ""


def test_a_section_is_a_position():
    """A Word chunk used to be able to name only the file."""
    assert chunk_location({"meta": {"section": "章节标题"}}) == "章节标题"
    assert chunk_location({"meta": {"heading": "章节标题"}}) == "章节标题"


def test_a_sheet_and_a_row_are_a_position():
    chunk = {"meta": {"anchor": make_anchor("sheet_row", sheet="台账", row=7)}}
    assert chunk_location(chunk) == "台账 第 7 行"
