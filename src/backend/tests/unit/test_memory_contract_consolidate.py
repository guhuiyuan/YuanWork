"""The consolidation engine's four counters.

* what `run()` actually counts
* that the de-duplication only counts and never merges
* that the conflict archiving is not implemented
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.consolidation.engine import ConsolidationEngine
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.episodic import EpisodicMemory
from yuanwork.memory.profile import ProfileStore


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def engine(store) -> ConsolidationEngine:
    return ConsolidationEngine(
        episodic_memory=EpisodicMemory(store),
        semantic_memory=None,  # type: ignore[arg-type]
        profile_store=ProfileStore(store),
        sqlite_store=store,
        llm_gateway=None,
    )


async def add(engine, content: str, role: str = "user", session: str = "s1"):
    return await engine.record_episode(session, role, content)


# --------------------------------------------------------------------------
# the counters
# --------------------------------------------------------------------------


async def test_an_empty_run_reports_zeros(engine):
    result = await engine.run()
    assert result["episodes_processed"] == 0
    assert result["duplicates_merged"] == 0
    assert result["profile_updates"] == 0
    assert result["conflicts_archived"] == 0
    assert result["duration_ms"] >= 0


async def test_the_processed_count_is_the_number_of_episodes(engine):
    await add(engine, "一")
    await add(engine, "二")
    result = await engine.run()
    assert result["episodes_processed"] == 2


async def test_the_profile_count_is_incremented_for_every_user_episode(engine):
    """The counter is incremented unconditionally — it counts the
    episodes *scanned*, not the profiles actually written."""
    await add(engine, "我喜欢喝茶")
    await add(engine, "今天天气不错")
    result = await engine.run()
    assert result["profile_updates"] == 2


async def test_the_conflict_archiving_is_not_implemented(engine):
    await add(engine, "一")
    result = await engine.run()
    assert result["conflicts_archived"] == 0


async def test_the_force_flag_is_accepted_and_ignored(engine):
    await add(engine, "一")
    without = await engine.run(reason="manual", force=False)
    with_force = await engine.run(reason="manual", force=True)
    assert {k: v for k, v in without.items() if k != "duration_ms"} == {
        k: v for k, v in with_force.items() if k != "duration_ms"
    }


async def test_the_reason_only_shows_up_in_the_log(engine):
    await add(engine, "一")
    a = await engine.run(reason="manual")
    b = await engine.run(reason="scheduled")
    assert a["episodes_processed"] == b["episodes_processed"]


# --------------------------------------------------------------------------
# the de-duplication
# --------------------------------------------------------------------------


async def test_identical_content_counts_as_a_duplicate(engine):
    await add(engine, "完全一样的内容")
    await add(engine, "完全一样的内容")
    result = await engine.run()
    assert result["duplicates_merged"] == 1


async def test_the_duplicate_is_not_deleted(engine):
    """The de-duplication only counts — nothing is merged or removed."""
    await add(engine, "完全一样的内容")
    await add(engine, "完全一样的内容")
    await engine.run()
    assert await engine.episodic.count() == 2


async def test_different_content_is_not_a_duplicate(engine):
    await add(engine, "内容一")
    await add(engine, "内容二")
    result = await engine.run()
    assert result["duplicates_merged"] == 0


async def test_the_dedup_hash_is_not_stable_across_processes():
    """The hash is Python's built-in `hash()`, which is randomised per
    process — so the de-duplication is not reproducible between runs."""
    import subprocess
    import sys

    script = "print(hash('同一段内容') != hash('同一段内容'))"
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True
    )
    assert out.stdout.strip() == "False"  # within one process it is stable

    # across processes the seed differs, so the value changes
    values = set()
    for _ in range(3):
        out = subprocess.run(
            [sys.executable, "-c", "print(hash('同一段内容'))"],
            capture_output=True, text=True,
        )
        values.add(out.stdout.strip())
    assert len(values) > 1, "the hash is stable across processes after all"


# --------------------------------------------------------------------------
# the recording
# --------------------------------------------------------------------------


async def test_recording_an_episode_writes_it(engine):
    episode_id = await add(engine, "内容")
    assert isinstance(episode_id, int)
    assert await engine.episodic.count() == 1


async def test_recording_an_assistant_turn_does_not_touch_the_profile(engine):
    await add(engine, "我是律师", role="assistant")
    assert await ProfileStore(engine.sqlite).list_all() == []
