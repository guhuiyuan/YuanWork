"""The profile store's own semantics.

The store is a thin wrapper over the relational one; what matters is the
overwrite behaviour and the expiry handling.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.profile import ProfileStore


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def profile(store) -> ProfileStore:
    return ProfileStore(store)


async def test_an_entry_round_trips(profile):
    await profile.set(
        "user.role", "律师", source_episode_id=None, confidence=0.9,
        expires_at=datetime(2030, 1, 1),
    )
    entry = await profile.get("user.role")
    assert entry["key"] == "user.role"
    assert entry["value"] == "律师"
    assert entry["confidence"] == 0.9
    assert entry["expires_at"] is not None


async def test_an_unknown_key_reads_as_none(profile):
    assert await profile.get("nope") is None


async def test_the_default_confidence_is_one(profile):
    await profile.set("user.role", "律师")
    entry = await profile.get("user.role")
    assert entry["confidence"] == 1.0


async def test_the_same_key_is_overwritten(profile):
    await profile.set("user.role", "律师", confidence=0.9)
    await profile.set("user.role", "会计", confidence=0.5)
    entries = await profile.list_all()
    assert len(entries) == 1
    assert entries[0]["value"] == "会计"
    assert entries[0]["confidence"] == 0.5


async def test_a_write_without_an_expiry_clears_the_existing_one(profile):
    """The upsert copies `expires_at` from the new value, so a write
    without one wipes it."""
    await profile.set("user.role", "律师", expires_at=datetime(2030, 1, 1))
    await profile.set("user.role", "会计")
    entry = await profile.get("user.role")
    assert entry["expires_at"] is None


async def test_the_entries_can_be_listed_with_a_prefix(profile):
    await profile.set("user.role", "律师")
    await profile.set("user.preference.like", "喝茶")
    await profile.set("other.thing", "x")
    assert len(await profile.list_all()) == 3
    assert len(await profile.list_all(prefix="user.")) == 2
    assert len(await profile.list_all(prefix="user.role")) == 1


async def test_the_entries_can_be_deleted(profile):
    await profile.set("user.role", "律师")
    assert await profile.delete("user.role") is True
    assert await profile.get("user.role") is None


async def test_deleting_an_unknown_key_reports_success(profile):
    """The store always reports success."""
    assert await profile.delete("nope") is True


async def test_several_entries_can_be_read_at_once(profile):
    await profile.set("a", "1")
    await profile.set("b", "2")
    found = await profile.get_many(["a", "b", "missing"])
    assert set(found) == {"a", "b"}
