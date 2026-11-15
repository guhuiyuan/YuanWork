"""TASK-010 — the embedding batch size on the ingestion path.

* AC-P-003 — a single embedding call submits at most 8 texts

The batch size is a hard-coded default on the embedder wrapper rather
than a setting, so this test pins both facts: the effective batch size
is 8, and the `embedding_batch_size` setting does not change it.
"""

from __future__ import annotations

from pathlib import Path

from yuanwork.memory.semantic import SemanticMemory


class FakeEmbedder:
    """Records the size of every batch it is handed."""

    def __init__(self, dim: int = 4):
        self.dim = dim
        self.batch_sizes: list[int] = []

    def encode(self, batch, return_dense=True, return_sparse=False, return_colbert_vecs=False):
        self.batch_sizes.append(len(batch))
        return {
            "dense_vecs": [[0.1] * self.dim for _ in batch],
            "lexical_weights": [{} for _ in batch],
        }


def make_memory(temp_dir: Path) -> SemanticMemory:
    """A semantic memory with a fake embedder — no model is loaded."""
    memory = SemanticMemory(data_dir=temp_dir / "lancedb")
    memory._embedder = FakeEmbedder()
    return memory


# --------------------------------------------------------------------------
# AC-P-003 — the batch size
# --------------------------------------------------------------------------


async def test_the_batch_size_default_is_eight(temp_dir: Path):
    memory = make_memory(temp_dir)
    await memory.embed_texts([f"文本{i}" for i in range(20)])
    assert memory._embedder.batch_sizes == [8, 8, 4]


async def test_a_short_list_is_one_batch(temp_dir: Path):
    memory = make_memory(temp_dir)
    await memory.embed_texts(["a", "b", "c"])
    assert memory._embedder.batch_sizes == [3]


async def test_an_empty_list_makes_no_call(temp_dir: Path):
    memory = make_memory(temp_dir)
    dense, sparse = await memory.embed_texts([])
    assert dense == []
    assert memory._embedder.batch_sizes == []


async def test_exactly_one_full_batch(temp_dir: Path):
    memory = make_memory(temp_dir)
    await memory.embed_texts([f"t{i}" for i in range(8)])
    assert memory._embedder.batch_sizes == [8]


async def test_the_batch_size_can_be_overridden_per_call(temp_dir: Path):
    memory = make_memory(temp_dir)
    await memory.embed_texts([f"t{i}" for i in range(10)], batch_size=3)
    assert memory._embedder.batch_sizes == [3, 3, 3, 1]


# --------------------------------------------------------------------------
# the setting is not wired up
# --------------------------------------------------------------------------


async def test_the_embedding_batch_size_setting_is_not_applied(temp_dir: Path):
    """`settings.embedding_batch_size` exists but nothing reads it — the
    effective batch size stays 8 whatever the setting says."""
    import inspect

    from yuanwork.config import YuanWorkSettings

    settings = YuanWorkSettings(data_dir=temp_dir / "data", embedding_batch_size=3)
    assert settings.embedding_batch_size == 3

    memory = make_memory(temp_dir)
    await memory.embed_texts([f"t{i}" for i in range(20)])
    assert memory._embedder.batch_sizes == [8, 8, 4]

    # and the ingestion path never passes a batch size, so the default wins
    source = inspect.getsource(SemanticMemory.add_chunks)
    assert "embed_texts(" in source
    assert "batch_size" not in source.split("embed_texts(")[1].split(")")[0]
