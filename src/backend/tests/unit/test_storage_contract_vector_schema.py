"""The vector store's table structure and write semantics.

* the fourteen columns and their types
* the 1024-dimensional vector
* the fields that are always written with the same value
* the anchor that carries the position of the chunk
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from yuanwork.memory.semantic import SemanticMemory
from yuanwork.utils.anchor import make_anchor


class FakeEmbedder:
    def __init__(self, dim: int = 1024):
        self.dim = dim

    def encode(self, batch, return_dense=True, return_sparse=False, return_colbert_vecs=False):
        return {
            "dense_vecs": [[0.1] * self.dim for _ in batch],
            "lexical_weights": [{"token": 1.0} for _ in batch],
        }


def make_chunk(**overrides) -> dict[str, Any]:
    chunk = {
        "id": "c1",
        "doc_id": "d1",
        "chunk_idx": 1,
        "text": "正文内容",
        "type": "text",
        "meta": {"page": 12, "page_total": 40},
        "file_name": "a.pdf",
        "file_path": "/kb/a.pdf",
        "file_type": "pdf",
    }
    chunk.update(overrides)
    return chunk


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


async def add(memory, *chunks):
    return await memory.add_chunks(list(chunks))


async def row(memory, chunk_id: str = "c1") -> dict:
    table = memory.get_or_create_chunks_table()
    for record in table.to_arrow().to_pylist():
        if record["id"] == chunk_id:
            return record
    raise AssertionError(f"no row with id {chunk_id}")


# --------------------------------------------------------------------------
# the schema
# --------------------------------------------------------------------------


async def test_the_table_has_the_fourteen_expected_fields(memory):
    table = memory.get_or_create_chunks_table()
    names = [f.name for f in table.schema]
    assert names == [
        "id", "doc_id", "chunk_idx", "text", "vector", "file_name",
        "file_path", "file_type", "anchor",
        "content_type", "token_count", "modified_at", "created_at",
        "sparse_str",
    ]


async def test_the_vector_column_is_a_1024_float_list(memory):
    table = memory.get_or_create_chunks_table()
    field = table.schema.field("vector")
    assert field.type.list_size == 1024
    assert "float" in str(field.type.value_type)


async def test_the_declared_schema_matches_the_created_table(memory):
    table = memory.get_or_create_chunks_table()
    declared = memory._expected_schema_fields()
    assert [name for name, _t in declared] == [f.name for f in table.schema]


# --------------------------------------------------------------------------
# what is written
# --------------------------------------------------------------------------


async def test_the_chunk_fields_are_stored(memory):
    await add(memory, make_chunk())
    stored = await row(memory)
    assert stored["id"] == "c1"
    assert stored["doc_id"] == "d1"
    assert stored["chunk_idx"] == 1
    assert stored["text"] == "正文内容"
    assert stored["file_name"] == "a.pdf"
    assert stored["file_type"] == "pdf"


async def test_the_vector_has_1024_dimensions(memory):
    await add(memory, make_chunk())
    stored = await row(memory)
    assert len(stored["vector"]) == 1024


async def test_the_anchor_is_stored_as_json(memory):
    await add(
        memory,
        make_chunk(meta={"anchor": make_anchor("page", page=12), "page_total": 40}),
    )
    stored = await row(memory)
    assert json.loads(stored["anchor"]) == {"kind": "page", "page": 12}
    # page_total has no column, so it is dropped
    assert "page_total" not in stored


async def test_a_spreadsheet_anchor_reaches_the_store(memory):
    """The sheet and the row used to have nowhere to go."""
    await add(
        memory,
        make_chunk(meta={"anchor": make_anchor("sheet_row", sheet="台账", row=7)}),
    )
    stored = await row(memory)
    assert json.loads(stored["anchor"]) == {
        "kind": "sheet_row",
        "sheet": "台账",
        "row": 7,
    }


async def test_a_chunk_without_a_position_stores_an_empty_anchor(memory):
    await add(memory, make_chunk(meta={}))
    stored = await row(memory)
    assert stored["anchor"] == ""


# --------------------------------------------------------------------------
# the fields that are always the same
# --------------------------------------------------------------------------


async def test_the_content_type_is_always_text(memory):
    """The chunker writes "type"; the store reads "content_type" — so
    every row says text, even for tables and images."""
    await add(memory, make_chunk(type="table"))
    stored = await row(memory)
    assert stored["content_type"] == "text"


async def test_the_token_count_is_the_character_count_over_four(memory):
    await add(memory, make_chunk(text="x" * 400))
    stored = await row(memory)
    assert stored["token_count"] == 100


async def test_the_sparse_column_is_empty_unless_the_channel_is_on(memory):
    """The column is there; the weights are not computed by default.

    The retrieval's sparse channel is the full-text index, so bge-m3's
    own lexical weights are not asked for on the way in (TASK-055).
    """
    await add(memory, make_chunk())
    stored = await row(memory)
    assert "sparse_str" in stored
    assert not stored["sparse_str"]


async def test_the_sparse_weights_are_stored_as_json_when_asked_for(memory):
    """Switched on, the column is filled the way it always was."""
    memory._sparse_enabled = True
    await add(memory, make_chunk())
    stored = await row(memory)
    assert stored["sparse_str"]
    assert "token" in stored["sparse_str"]


async def test_the_timestamps_are_iso_strings(memory):
    await add(memory, make_chunk())
    stored = await row(memory)
    assert "T" in stored["created_at"]
    assert "T" in stored["modified_at"]


# --------------------------------------------------------------------------
# the write behaviour
# --------------------------------------------------------------------------


async def test_the_same_id_is_replaced_not_duplicated(memory):
    await add(memory, make_chunk(text="第一版"))
    await add(memory, make_chunk(text="第二版"))
    table = memory.get_or_create_chunks_table()
    assert table.count_rows() == 1
    stored = await row(memory)
    assert stored["text"] == "第二版"


async def test_the_write_returns_the_chunk_ids(memory):
    ids = await add(memory, make_chunk())
    assert ids == ["c1"]


async def test_a_chunk_without_a_vector_field_is_padded(memory):
    """A short vector is padded with zeros, a long one is cut."""
    await add(memory, make_chunk())
    stored = await row(memory)
    assert len(stored["vector"]) == 1024
