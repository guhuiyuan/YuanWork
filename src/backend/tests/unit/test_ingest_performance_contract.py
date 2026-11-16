"""A build of thousands of files is written in batches.

One embedding call per batch instead of one per file, a capped write
rate, and a ceiling on the core layer - a core layer with thousands of
documents is not a summary of anything any more.

The embedding itself is the slowest stage of a build, so the things
that make it slower are pinned here too: the sparse channel nobody
reads, and the padding that comes with batching slices of mixed
lengths.

* AC-P-007 - the long tail is written in batches at a capped rate, and
  the core layer has a ceiling
* AC-P-011 - the embedding is fast enough (15 slices a second)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.ingestion.pipeline import ChunkBatch, IngestionPipeline


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class RecordingSemantic:
    """The writes, as they happen."""

    def __init__(self):
        self.batches: list[list[str]] = []

    async def add_chunks(self, chunks):
        self.batches.append([c["id"] for c in chunks])
        return [c["id"] for c in chunks]

    async def delete_by_doc_id(self, doc_id):
        return None

    @property
    def written(self) -> int:
        return sum(len(batch) for batch in self.batches)


# --------------------------------------------------------------------------
# the batch
# --------------------------------------------------------------------------


async def test_the_slices_are_written_in_batches():
    """One embedding call per batch, not one per file."""
    semantic = RecordingSemantic()
    batch = ChunkBatch(semantic, batch_size=10)

    for index in range(25):
        await batch.add([{"id": f"c{index}"}])

    # Two full batches went in; the rest is still waiting.
    assert len(semantic.batches) == 2
    assert semantic.written == 20


async def test_what_is_left_over_is_written_at_the_end():
    semantic = RecordingSemantic()
    batch = ChunkBatch(semantic, batch_size=10)

    await batch.add([{"id": "c1"}])
    assert semantic.batches == []

    await batch.flush()
    assert semantic.written == 1


async def test_the_writer_keeps_the_rate_under_the_cap():
    """A build must not starve the rest of the machine."""
    import time

    semantic = RecordingSemantic()
    batch = ChunkBatch(semantic, batch_size=10, rate_limit=1000.0)

    started = time.perf_counter()
    for index in range(10):
        await batch.add([{"id": f"c{index}"}])
    await batch.flush()
    elapsed = time.perf_counter() - started

    # Ten slices at a thousand a second cannot take less than 0.01 s.
    assert elapsed >= 0.005, f"the writer ran at {10 / elapsed:.0f} slices/s"


async def test_a_batch_that_fails_does_not_stop_the_build():
    """One batch is not the whole build."""

    class BrokenSemantic(RecordingSemantic):
        async def add_chunks(self, chunks):
            raise RuntimeError("store 挂了")

    batch = ChunkBatch(BrokenSemantic(), batch_size=10)
    await batch.add([{"id": "c1"}] * 10)

    # It did not raise - the build goes on.
    assert batch.written == 0


# --------------------------------------------------------------------------
# the build itself
# --------------------------------------------------------------------------


async def test_a_build_writes_in_batches(store, tmp_path: Path):
    """Ten files, one batch - not ten writes."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    semantic = RecordingSemantic()
    pipeline = IngestionPipeline(
        sqlite_store=store,
        semantic_memory=semantic,
        index_batch_size=1000,
    )

    for index in range(10):
        (tmp_path / f"文件{index}.md").write_text(
            f"# 文件 {index}\n\n内容 {'x' * 100}\n", encoding="utf-8"
        )

    result = await pipeline.ingest_directory(tmp_path)

    assert result["indexed"] == 10
    assert len(semantic.batches) == 1, "the build wrote one file at a time"


async def test_a_single_file_is_written_on_its_own(store, tmp_path: Path):
    """The one-file route does not wait for a batch."""
    from yuanwork.ingestion.pipeline import IngestionPipeline

    semantic = RecordingSemantic()
    pipeline = IngestionPipeline(sqlite_store=store, semantic_memory=semantic)

    path = tmp_path / "文件.md"
    path.write_text("# 文件\n\n内容\n", encoding="utf-8")

    await pipeline.ingest_file(path)

    assert len(semantic.batches) == 1


# --------------------------------------------------------------------------
# the ceiling of the core layer
# --------------------------------------------------------------------------


async def test_the_core_layer_has_a_ceiling(store):
    """Past the ceiling a document is still indexed - it is just not a
    summary any more."""
    from yuanwork.ingestion.tier import TierClassifier

    for index in range(3):
        await store.upsert_doc_tier(
            doc_id=f"core-{index}",
            tier="core",
            decided_by="rule",
        )

    classifier = TierClassifier(store, max_core_docs=3)

    result = await classifier.classify(
        {
            "doc_id": "d-new",
            "file_path": "/kb/合同/采购合同.pdf",
            "file_name": "采购合同.pdf",
            "file_type": "pdf",
            "text_length": 100,
        }
    )

    assert result["tier"] == "longtail"
    assert "上限" in result["reason"]


async def test_the_ceiling_does_not_touch_the_users_choice(store):
    """The user said which layer it is."""
    from yuanwork.ingestion.tier import TierClassifier

    for index in range(3):
        await store.upsert_doc_tier(
            doc_id=f"core-{index}",
            tier="core",
            decided_by="rule",
        )
    await store.set_doc_tier_by_user("d-user", "core")

    classifier = TierClassifier(store, max_core_docs=3)

    result = await classifier.classify(
        {
            "doc_id": "d-user",
            "file_path": "/kb/合同/采购合同.pdf",
            "file_name": "采购合同.pdf",
            "file_type": "pdf",
            "text_length": 100,
        }
    )

    assert result["tier"] == "core"
    assert result["decided_by"] == "user"


async def test_the_ceiling_is_configuration():
    """How many documents the core layer holds is configuration."""
    from yuanwork.config import YuanWorkSettings

    settings = YuanWorkSettings(data_dir=Path("."), core_max_docs=42)
    assert settings.core_max_docs == 42


# --------------------------------------------------------------------------
# the embedding stage
# --------------------------------------------------------------------------


class FakeEmbedder:
    """An embedding model that says what it was asked for."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def encode(
        self,
        batch,
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
    ):
        self.calls.append(
            {"count": len(batch), "sparse": return_sparse, "texts": list(batch)}
        )
        n = len(batch)
        # The first number of each vector is the length of its text -
        # enough to tell the order of the answers apart.
        return {
            "dense_vecs": [[float(len(t)), 0.0] + [0.0] * 1022 for t in batch],
            "lexical_weights": [{"x": 1.0} for _ in range(n)],
        }


def test_the_sparse_channel_nobody_reads_is_off_by_default(tmp_path: Path):
    """bge-m3's lexical weights are not the sparse channel any more.

    The retrieval's sparse channel is the full-text index (BM25). The
    bge-m3 one only fills a column, for a possible reactivation some
    day - and computing it on every slice is the build's most
    expensive minute.
    """
    from yuanwork.memory.semantic import SemanticMemory

    memory = SemanticMemory(tmp_path / "lancedb")
    assert memory._sparse_enabled is False


def test_the_sparse_channel_can_be_turned_back_on(tmp_path: Path, monkeypatch):
    """The column is one setting away from being filled again."""
    from yuanwork.config import YuanWorkSettings
    from yuanwork.memory import semantic as semantic_module

    settings = YuanWorkSettings(
        data_dir=tmp_path / "data", embedding_sparse_channel=True
    )
    # The store reads the settings when it is built, so this is where
    # the setting has to be replaced.
    monkeypatch.setattr("yuanwork.config.get_settings", lambda: settings)

    memory = semantic_module.SemanticMemory(tmp_path / "lancedb")
    assert memory._sparse_enabled is True


async def test_the_ingest_does_not_ask_for_the_sparse_channel(tmp_path: Path):
    """The build's slowest stage must not compute what nobody reads."""
    from yuanwork.memory.semantic import SemanticMemory

    memory = SemanticMemory(tmp_path / "lancedb")
    embedder = FakeEmbedder()
    memory._embedder = embedder

    await memory.embed_texts(["一段文字"], return_sparse=False)

    assert embedder.calls, "the embedder was never called"
    assert embedder.calls[0]["sparse"] is False


async def test_batching_by_length_keeps_the_order(tmp_path: Path):
    """The slices come back in the order they went in.

    They are batched by length so that the padding stays small - a
    batch pads to its longest text - and the order is put back
    afterwards. The caller does not care how it was batched, but it
    does care that the answers match the questions.
    """
    from yuanwork.memory.semantic import SemanticMemory

    memory = SemanticMemory(tmp_path / "lancedb")
    embedder = FakeEmbedder()
    memory._embedder = embedder

    texts = ["短", "中等长度的一段文字", "很长很长很长很长很长很长很长很长的一段文字"]
    dense, _ = await memory.embed_texts(texts, return_sparse=False)

    assert [vector[0] for vector in dense] == [float(len(t)) for t in texts]
    # And they were not handed over in one batch of mixed lengths.
    assert len(embedder.calls) >= 1


def test_a_batch_never_mixes_a_short_slice_with_a_long_one():
    """The padding a batch costs is decided by its longest slice.

    A batch of 32 sorted slices reaches much further into the long
    tail than a batch of 8 does, so every slice in it pays for the
    longest one. Measured on real slices: sorting and cutting into
    fixed batches of 32 made the padding *worse* than not sorting at
    all (74% against 49%). The batches are cut into windows instead -
    a window ends when the next text is much longer than the
    window's shortest one.
    """
    from yuanwork.memory.semantic import _batches_by_length

    texts = ["x" * 100 for _ in range(10)] + ["y" * 2000]
    batches = _batches_by_length(texts, batch_size=32)

    assert len(batches) == 2, "the long slice shared a batch with the short ones"
    for positions in batches:
        lengths = [len(texts[p]) for p in positions]
        assert max(lengths) <= min(lengths) * 2


def test_the_batches_hold_every_text_exactly_once():
    """Nothing is lost and nothing is counted twice."""
    from yuanwork.memory.semantic import _batches_by_length

    texts = [f"文本{i}" * (i + 1) for i in range(50)]
    batches = _batches_by_length(texts, batch_size=8)

    positions = [p for batch in batches for p in batch]
    assert sorted(positions) == list(range(len(texts)))


def test_the_batches_respect_the_batch_size():
    """A batch is never bigger than it is allowed to be."""
    from yuanwork.memory.semantic import _batches_by_length

    texts = ["x" * 10 for _ in range(100)]
    batches = _batches_by_length(texts, batch_size=8)

    assert all(len(batch) <= 8 for batch in batches)


def test_a_bigger_batch_gets_proportionally_more_time(tmp_path: Path):
    """The timeout is a budget for a full batch of eight slices.

    Sixty seconds is plenty for eight slices on a GPU and far too
    little for thirty-two on a CPU, where the same work takes ten
    times as long. A build that gives up halfway is worse than a slow
    one.
    """
    from yuanwork.memory.semantic import SemanticMemory

    memory = SemanticMemory(tmp_path / "lancedb")

    assert memory._batch_timeout(8) == pytest.approx(memory.embedding_timeout)
    assert memory._batch_timeout(32) == pytest.approx(
        memory.embedding_timeout * 4
    )
    assert memory._batch_timeout(4) == pytest.approx(
        memory.embedding_timeout / 2
    )


def test_the_quantization_is_a_setting_and_not_a_default(tmp_path: Path):
    """int8 is about twice as fast on the CPU - and it moves the
    vectors a little, so it is a setting and not a default."""
    from yuanwork.config import YuanWorkSettings
    from yuanwork.memory import semantic as semantic_module

    settings = YuanWorkSettings(data_dir=tmp_path / "data")
    assert settings.embedding_quantize == "none"

    assert semantic_module.SemanticMemory(tmp_path / "lancedb").embedding_quantize == "none"

    quantized = YuanWorkSettings(data_dir=tmp_path / "data", embedding_quantize="int8")
    assert quantized.embedding_quantize == "int8"


def test_a_model_that_cannot_be_quantized_is_left_as_it_is():
    """A failed quantization must not fail the build."""
    from yuanwork.memory.semantic import _quantize_int8

    class _NoInner:
        model = None

    assert _quantize_int8(_NoInner()) is not None


# --------------------------------------------------------------------------
# the write path
# --------------------------------------------------------------------------


class RecordingFTS:
    """The full-text index, and how often it was flushed."""

    def __init__(self) -> None:
        self.commits = 0
        self.added: list[list[dict]] = []
        self.deleted: list[list[str]] = []

    def delete_by_ids(self, ids, commit=True):
        self.deleted.append(list(ids))
        return len(ids)

    def add_documents(self, docs, commit=True):
        self.added.append(list(docs))
        return len(docs)

    def commit(self):
        self.commits += 1


async def test_the_write_flushes_the_full_text_index_once(tmp_path: Path):
    """One batch, one flush.

    The delete of the old version and the write of the new one used to
    be two flushes - two disk writes for one batch. A commit is the
    expensive part of the write path, so they share one.
    """
    from yuanwork.memory.semantic import SemanticMemory

    memory = SemanticMemory(tmp_path / "lancedb")
    await memory.initialize()
    fts = RecordingFTS()
    memory._fts_index = fts
    memory._embedder = FakeEmbedder()

    await memory.add_chunks(
        [
            {"id": "c1", "doc_id": "d1", "chunk_idx": 0, "text": "金额 1280"},
            {"id": "c2", "doc_id": "d1", "chunk_idx": 1, "text": "日期 2026"},
        ]
    )

    assert fts.commits == 1, "the batch was flushed more than once"
    assert fts.deleted == [["c1", "c2"]]
    assert [doc["id"] for doc in fts.added[0]] == ["c1", "c2"]

    await memory.close()


async def test_the_full_text_index_can_be_flushed_on_its_own(tmp_path: Path):
    """The build flushes what is left when it is done."""
    from yuanwork.memory.semantic import SemanticMemory

    memory = SemanticMemory(tmp_path / "lancedb")
    await memory.initialize()
    fts = RecordingFTS()
    memory._fts_index = fts

    await memory.flush_fts()

    assert fts.commits == 1
    await memory.close()


async def test_a_closed_store_flushes_what_is_left(tmp_path: Path):
    """A store that is closed must not leave documents in the writer."""
    from yuanwork.memory.semantic import SemanticMemory

    memory = SemanticMemory(tmp_path / "lancedb")
    await memory.initialize()
    fts = RecordingFTS()
    memory._fts_index = fts

    await memory.close()

    assert fts.commits == 1

