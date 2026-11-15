"""Where a chunk is: the position has to survive the whole way.

The position of a chunk used to live in three fixed columns - page,
slide_number and section. Everything else the parsers produced was
dropped: most notably a spreadsheet's sheet name and row, which meant a
citation for an Excel chunk could only name the file.

The anchor fixed that: one structure with a kind and the coordinates that
kind defines, carried from the parser through the chunk into the store
and back out into the citation.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from yuanwork.ingestion.parser.xlsx_parser import XlsxParser
from yuanwork.memory.semantic import SemanticMemory
from yuanwork.utils.anchor import anchor_of
from yuanwork.utils.chunk_meta import chunk_location, chunk_meta


class FakeEmbedder:
    def encode(self, batch, return_dense=True, return_sparse=False, return_colbert_vecs=False):
        return {
            "dense_vecs": [[0.1] * 1024 for _ in batch],
            "lexical_weights": [{} for _ in batch],
        }


@pytest.fixture
async def memory(tmp_path: Path):
    mem = SemanticMemory(data_dir=tmp_path / "lancedb")
    mem._embedder = FakeEmbedder()
    await mem.initialize()
    yield mem
    try:
        await mem.close()
    except Exception:
        pass


async def store_chunk(memory, blocks, index: int = 0) -> dict:
    """Put one parsed block into the store and return the stored row."""
    block = blocks[index]
    chunk = {
        "id": "c1",
        "doc_id": "doc1",
        "chunk_idx": 1,
        "text": block.content,
        "type": block.type,
        "meta": dict(block.meta),
        "file_name": "sheet.xlsx",
        "file_path": "/kb/sheet.xlsx",
        "file_type": "xlsx",
    }
    await memory.add_chunks([chunk])
    table = memory.get_or_create_chunks_table()
    return table.to_arrow().to_pylist()[0]


# --------------------------------------------------------------------------
# what the parser produces
# --------------------------------------------------------------------------


async def test_the_parser_produces_the_sheet_information(tmp_path: Path):
    from unit.ingestion_fixtures import make_xlsx_file

    blocks = await XlsxParser().parse(make_xlsx_file(tmp_path), "doc1")
    assert blocks
    meta = blocks[0].meta
    assert meta["sheet_name"] == "进度表"
    assert "sheet_index" in meta
    assert "sheet_total" in meta
    assert "row_count" in meta
    assert meta["row_index"] >= 1


# --------------------------------------------------------------------------
# what reaches the store
# --------------------------------------------------------------------------


async def test_the_sheet_information_reaches_the_vector_store(memory, tmp_path: Path):
    from unit.ingestion_fixtures import make_xlsx_file

    blocks = await XlsxParser().parse(make_xlsx_file(tmp_path), "doc1")
    stored = await store_chunk(memory, blocks)

    anchor = json.loads(stored["anchor"])
    assert anchor["kind"] == "sheet_row"
    assert anchor["sheet"] == "进度表"
    assert anchor["row"] == blocks[0].meta["row_index"]


async def test_a_spreadsheet_citation_can_name_the_sheet_and_the_row(
    memory, tmp_path: Path
):
    from unit.ingestion_fixtures import make_xlsx_file

    blocks = await XlsxParser().parse(make_xlsx_file(tmp_path), "doc1")
    stored = await store_chunk(memory, blocks)

    assert chunk_location(stored) == f"进度表 第 {blocks[0].meta['row_index']} 行"
    meta = chunk_meta(stored)
    assert meta["sheet"] == "进度表"
    assert meta["row"] == blocks[0].meta["row_index"]


# --------------------------------------------------------------------------
# the position survives the round trip
# --------------------------------------------------------------------------


async def test_the_stored_row_reads_its_anchor_back(memory, tmp_path: Path):
    from unit.ingestion_fixtures import make_xlsx_file

    blocks = await XlsxParser().parse(make_xlsx_file(tmp_path), "doc1")
    stored = await store_chunk(memory, blocks)

    anchor = anchor_of(stored)
    assert anchor is not None
    assert anchor["kind"] == "sheet_row"


# --------------------------------------------------------------------------
# the fields that are still not positions
# --------------------------------------------------------------------------


def test_the_location_helper_does_not_cover_the_heading_level():
    from yuanwork.utils.chunk_meta import LOCATION_FIELDS

    assert "heading_level" not in LOCATION_FIELDS


def test_the_store_has_one_position_column(memory):
    table = memory.get_or_create_chunks_table()
    names = {f.name for f in table.schema}
    location_columns = names & {"page", "slide_number", "section", "anchor"}
    assert location_columns == {"anchor"}
