"""The vector store's schema migration.

The migration policy is *add the column, do not back-fill* — which
means an old knowledge base keeps null in the new columns until the
document is re-ingested.

The anchor replaced the three fixed location columns, so an old table
gets an `anchor` column next to the `page` / `slide_number` / `section`
columns it already has. The old columns stay: a table cannot lose a
column, and the rows in it are still read through them.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pyarrow as pa
import pytest

from yuanwork.memory.semantic import SemanticMemory


class FakeEmbedder:
    def encode(self, batch, return_dense=True, return_sparse=False, return_colbert_vecs=False):
        return {
            "dense_vecs": [[0.1] * 1024 for _ in batch],
            "lexical_weights": [{} for _ in batch],
        }


def v1_schema() -> pa.Schema:
    """The v1 schema: everything except `sparse_str`."""
    return pa.schema([
        pa.field("id", pa.string()),
        pa.field("doc_id", pa.string()),
        pa.field("chunk_idx", pa.int32()),
        pa.field("text", pa.string()),
        pa.field("vector", pa.list_(pa.float32(), 1024)),
        pa.field("file_name", pa.string()),
        pa.field("file_path", pa.string()),
        pa.field("file_type", pa.string()),
        pa.field("page", pa.int32()),
        pa.field("slide_number", pa.int32()),
        pa.field("section", pa.string()),
        pa.field("content_type", pa.string()),
        pa.field("token_count", pa.int32()),
        pa.field("modified_at", pa.string()),
        pa.field("created_at", pa.string()),
    ])


@pytest.fixture
def old_table(tmp_path: Path):
    """A vector store that still has the v1 schema."""
    import lancedb

    data_dir = tmp_path / "lancedb"
    db = lancedb.connect(str(data_dir))
    table = db.create_table("chunks", schema=v1_schema(), mode="create")
    table.add([
        {
            "id": "old-1", "doc_id": "d1", "chunk_idx": 1, "text": "旧数据",
            "vector": [0.1] * 1024, "file_name": "a.md", "file_path": "/a.md",
            "file_type": "md", "page": None, "slide_number": None,
            "section": None, "content_type": "text", "token_count": 3,
            "modified_at": "2026-01-01", "created_at": "2026-01-01",
        }
    ])
    return data_dir


async def open_store(data_dir: Path) -> SemanticMemory:
    memory = SemanticMemory(data_dir=data_dir)
    memory._embedder = FakeEmbedder()
    await memory.initialize()
    return memory


async def test_the_missing_column_is_added(old_table: Path):
    memory = await open_store(old_table)
    table = memory.get_or_create_chunks_table()
    names = [f.name for f in table.schema]
    assert "sparse_str" in names
    # the anchor is added next to the columns the old table still has
    assert "anchor" in names
    assert len(names) == 17
    await memory.close()


async def test_the_old_rows_are_not_back_filled(old_table: Path):
    """The policy is "add the column", not "fill it in"."""
    memory = await open_store(old_table)
    table = memory.get_or_create_chunks_table()
    rows = table.to_arrow().to_pylist()
    assert len(rows) == 1
    assert rows[0]["sparse_str"] is None
    assert rows[0]["anchor"] is None
    await memory.close()


async def test_the_old_rows_keep_their_data(old_table: Path):
    memory = await open_store(old_table)
    table = memory.get_or_create_chunks_table()
    row = table.to_arrow().to_pylist()[0]
    assert row["id"] == "old-1"
    assert row["text"] == "旧数据"
    await memory.close()


async def test_a_fresh_table_needs_no_migration(tmp_path: Path):
    memory = await open_store(tmp_path / "lancedb")
    table = memory.get_or_create_chunks_table()
    assert len(table.schema) == 14
    await memory.close()


async def test_the_migration_is_driven_by_the_field_names():
    """`CHUNKS_SCHEMA_VERSION` is declared at module level, but the
    migration compares the field list — not the version number."""
    from yuanwork.memory import semantic as semantic_module

    assert semantic_module.CHUNKS_SCHEMA_VERSION == 2
    expected = semantic_module.SemanticMemory._expected_schema_fields(None)
    assert len(expected) == 14
    assert ("sparse_str", "string") in expected
    assert ("anchor", "string") in expected
