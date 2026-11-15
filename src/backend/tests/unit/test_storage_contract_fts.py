"""The full-text index as the vector store's mirror.

The index is written after the vector table and rebuilt only when it is
completely empty. This pins the mirroring, the rebuild trigger and what
happens when the index write fails.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from yuanwork.memory.semantic import SemanticMemory


class FakeEmbedder:
    def encode(self, batch, return_dense=True, return_sparse=False, return_colbert_vecs=False):
        return {
            "dense_vecs": [[0.1] * 1024 for _ in batch],
            "lexical_weights": [{} for _ in batch],
        }


def make_chunk(cid: str, text: str) -> dict:
    return {
        "id": cid,
        "doc_id": "d1",
        "chunk_idx": 1,
        "text": text,
        "type": "text",
        "meta": {},
        "file_name": "a.md",
        "file_path": "/kb/a.md",
        "file_type": "md",
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


# --------------------------------------------------------------------------
# the schema
# --------------------------------------------------------------------------


async def test_the_index_has_a_schema(memory):
    index = memory._get_fts_index()
    assert index._schema is not None


async def test_the_id_field_is_matched_exactly(memory):
    """The id is stored raw, so deleting by id removes exactly that
    document — a tokenized id would match a prefix."""
    await memory.add_chunks([
        make_chunk("c1", "发票 金额"), make_chunk("c12", "合同 条款"),
    ])
    index = memory._get_fts_index()
    index.delete_by_ids(["c1"])
    assert index.count() == 1
    assert index.search("合同")


async def test_the_doc_id_field_is_matched_exactly(memory):
    await memory.add_chunks([
        make_chunk("c1", "发票 金额"), make_chunk("c2", "合同 条款"),
    ])
    index = memory._get_fts_index()
    index.delete_by_doc_id("d1")
    assert index.count() == 0


async def test_the_index_directory_lives_under_the_data_directory(memory):
    index = memory._get_fts_index()
    assert index.index_dir.name == "fts"
    assert index.index_dir.parent == memory.data_dir


# --------------------------------------------------------------------------
# the mirroring
# --------------------------------------------------------------------------


async def test_a_written_chunk_is_mirrored_into_the_index(memory):
    await memory.add_chunks([make_chunk("c1", "发票 金额 1280")])
    index = memory._get_fts_index()
    assert index.count() == 1
    assert index.search("1280")


async def test_several_chunks_are_all_mirrored(memory):
    await memory.add_chunks([
        make_chunk("c1", "发票 金额"), make_chunk("c2", "合同 条款"),
    ])
    index = memory._get_fts_index()
    assert index.count() == 2


async def test_the_index_is_created_lazily(memory):
    """Before the first use there is no index directory at all."""
    assert memory._fts_index is None
    memory._get_fts_index()
    assert memory._fts_index is not None


async def test_an_empty_index_is_rebuilt_from_the_vector_store(memory, tmp_path):
    """A knowledge base that was written before the index existed gets
    one on first access."""
    import shutil

    await memory.add_chunks([make_chunk("c1", "发票 金额 1280")])

    # drop the index entirely, as a v3 store would be
    memory._get_fts_index().close()
    memory._fts_index = None
    shutil.rmtree(memory.data_dir / "fts", ignore_errors=True)

    memory._fts_needs_reindex = True
    rebuilt = memory._get_fts_index()
    assert rebuilt.count() == 1
    assert rebuilt.search("1280")


async def test_a_partially_missing_index_is_not_rebuilt(memory):
    """The rebuild only fires when the index is *completely* empty."""
    await memory.add_chunks([
        make_chunk("c1", "发票 金额"), make_chunk("c2", "合同 条款"),
    ])
    index = memory._get_fts_index()
    index.delete_by_ids(["c1"])
    assert index.count() == 1

    memory._fts_needs_reindex = True
    memory._fts_index = None
    memory._get_fts_index()
    assert memory._get_fts_index().count() == 1


# --------------------------------------------------------------------------
# the failure path
# --------------------------------------------------------------------------


async def test_a_failing_index_write_does_not_lose_the_vector_row(memory, monkeypatch):
    """The index is a mirror: if it cannot be written, the chunk is still
    in the vector store."""
    index = memory._get_fts_index()

    def boom(docs):
        raise RuntimeError("索引写不进去")

    monkeypatch.setattr(index, "add_documents", boom)

    await memory.add_chunks([make_chunk("c1", "发票 金额")])

    table = memory.get_or_create_chunks_table()
    assert table.count_rows() == 1


async def test_a_failing_index_delete_does_not_fail_the_delete(memory, monkeypatch):
    index = memory._get_fts_index()

    def boom(doc_id):
        raise RuntimeError("索引删不掉")

    monkeypatch.setattr(index, "delete_by_doc_id", boom)
    # the failure is swallowed: the vector rows are still deleted
    removed = await memory.delete_by_doc_id("d1")
    assert removed >= 0
    table = memory.get_or_create_chunks_table()
    assert table.count_rows() == 0
