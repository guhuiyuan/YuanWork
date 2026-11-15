"""A build of thousands of files is written in batches.

One embedding call per batch instead of one per file, a capped write
rate, and a ceiling on the core layer - a core layer with thousands of
documents is not a summary of anything any more.

* AC-P-007 - the long tail is written in batches at a capped rate, and
  the core layer has a ceiling
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
