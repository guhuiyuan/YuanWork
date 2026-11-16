"""The embedding batch size on the ingestion path.

* the batch size is configuration, and the default is the measured one

The batch size used to be a hard-coded 8: the model read eight slices
at a time whatever the setting said. On the CPU that is the single
biggest lever on how long a build takes - the same work, the same
padding, but the call overhead spread over more slices (measured on
real slices: 8 at a time gives 4 a second, 32 gives 13, 64 gives 16).
So it is a setting now, and the default is 32.
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


def make_memory(temp_dir: Path, **settings) -> SemanticMemory:
    """A semantic memory with a fake embedder — no model is loaded."""
    memory = SemanticMemory(data_dir=temp_dir / "lancedb")
    memory._embedder = FakeEmbedder()
    return memory


# --------------------------------------------------------------------------
# the batch size
# --------------------------------------------------------------------------


async def test_the_batch_size_default_is_thirty_two(temp_dir: Path):
    memory = make_memory(temp_dir)
    await memory.embed_texts([f"文本{i}" for i in range(40)])
    assert memory._embedder.batch_sizes == [32, 8]


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
    await memory.embed_texts([f"t{i}" for i in range(32)])
    assert memory._embedder.batch_sizes == [32]


async def test_the_batch_size_can_be_overridden_per_call(temp_dir: Path):
    memory = make_memory(temp_dir)
    await memory.embed_texts([f"t{i}" for i in range(10)], batch_size=3)
    assert memory._embedder.batch_sizes == [3, 3, 3, 1]


# --------------------------------------------------------------------------
# the setting is wired up
# --------------------------------------------------------------------------


async def test_the_embedding_batch_size_setting_is_applied(temp_dir: Path):
    """`settings.embedding_batch_size` decides how many slices the model
    reads at once."""
    from yuanwork.config import YuanWorkSettings, reset_settings

    settings = YuanWorkSettings(data_dir=temp_dir / "data", embedding_batch_size=3)
    assert settings.embedding_batch_size == 3

    reset_settings()
    try:
        import yuanwork.config as config_module

        config_module._settings = settings

        memory = make_memory(temp_dir)
        await memory.embed_texts([f"t{i}" for i in range(20)])
        assert memory._embedder.batch_sizes == [3, 3, 3, 3, 3, 3, 2]
    finally:
        reset_settings()


async def test_the_ingestion_path_does_not_pass_a_batch_size_of_its_own(temp_dir: Path):
    """The write path hands over a whole batch and lets the store decide
    how to cut it up - one place to change, one place to measure."""
    import inspect

    source = inspect.getsource(SemanticMemory.add_chunks)
    assert "embed_texts(" in source
    assert "batch_size" not in source.split("embed_texts(")[1].split(")")[0]
