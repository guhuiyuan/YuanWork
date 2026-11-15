"""The profile store and the two extraction paths.

* the profile row and its overwrite semantics
* the model path and its constraints
* the pattern-matching fallback and its looser constraints
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from yuanwork.consolidation.engine import ConsolidationEngine
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.episodic import EpisodicMemory
from yuanwork.memory.profile import ProfileStore


class FakeLLM:
    def __init__(self, content: str = "[]"):
        self.content = content
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        return type("R", (), {"content": self.content})()


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def profile(store) -> ProfileStore:
    return ProfileStore(store)


def make_engine(store, llm=None) -> ConsolidationEngine:
    return ConsolidationEngine(
        episodic_memory=EpisodicMemory(store),
        semantic_memory=None,  # type: ignore[arg-type]
        profile_store=ProfileStore(store),
        sqlite_store=store,
        llm_gateway=llm,
    )


# --------------------------------------------------------------------------
# the profile row
# --------------------------------------------------------------------------


async def test_a_profile_entry_round_trips(profile):
    await profile.set("user.role", "律师", source_episode_id=None, confidence=0.9)
    entry = await profile.get("user.role")
    assert entry["value"] == "律师"
    assert entry["confidence"] == 0.9


async def test_the_same_key_is_overwritten(profile):
    await profile.set("user.role", "律师", confidence=0.9)
    await profile.set("user.role", "会计", confidence=0.5)
    entry = await profile.get("user.role")
    assert entry["value"] == "会计"


async def test_an_unknown_key_reads_as_none(profile):
    assert await profile.get("nope") is None


async def test_the_entries_can_be_listed_by_prefix(profile):
    await profile.set("user.role", "律师")
    await profile.set("user.preference.like", "喝茶")
    await profile.set("other.thing", "x")
    listed = await profile.list_all(prefix="user.")
    assert len(listed) == 2


async def test_the_entries_can_be_deleted(profile):
    await profile.set("user.role", "律师")
    assert await profile.delete("user.role") is True


async def test_the_many_entries_can_be_read_at_once(profile):
    await profile.set("a", "1")
    await profile.set("b", "2")
    found = await profile.get_many(["a", "b", "missing"])
    assert set(found) == {"a", "b"}


# --------------------------------------------------------------------------
# the model path
# --------------------------------------------------------------------------


async def test_the_model_path_writes_the_facts(store):
    llm = FakeLLM(json.dumps([
        {"key": "user.role", "value": "律师"},
        {"key": "user.preference.like", "value": "喝茶"},
    ]))
    engine = make_engine(store, llm)
    await engine.record_episode("s1", "user", "我是律师，喜欢喝茶")

    entries = await ProfileStore(store).list_all()
    assert len(entries) == 2
    assert entries[0]["confidence"] == 0.75


async def test_the_model_path_skips_a_too_long_key(store):
    llm = FakeLLM(json.dumps([{"key": "k" * 61, "value": "值"}]))
    engine = make_engine(store, llm)
    await engine.record_episode("s1", "user", "内容")
    assert await ProfileStore(store).list_all() == []


async def test_the_model_path_skips_a_too_long_value(store):
    llm = FakeLLM(json.dumps([{"key": "user.role", "value": "值" * 201}]))
    engine = make_engine(store, llm)
    await engine.record_episode("s1", "user", "内容")
    assert await ProfileStore(store).list_all() == []


@pytest.mark.parametrize("sensitive", ["身份证", "手机号", "密码", "ssn", "password"])
async def test_the_model_path_skips_sensitive_values(store, sensitive: str):
    llm = FakeLLM(json.dumps([{"key": "user.secret", "value": f"我的{sensitive}是123"}]))
    engine = make_engine(store, llm)
    await engine.record_episode("s1", "user", "内容")
    assert await ProfileStore(store).list_all() == []


async def test_a_non_json_answer_falls_back_to_the_patterns(store):
    llm = FakeLLM("这不是 JSON")
    engine = make_engine(store, llm)
    await engine.record_episode("s1", "user", "我喜欢喝绿茶")
    entries = await ProfileStore(store).list_all()
    assert len(entries) == 1
    assert entries[0]["confidence"] == 0.6


async def test_a_json_object_instead_of_an_array_falls_back(store):
    llm = FakeLLM(json.dumps({"key": "user.role"}))
    engine = make_engine(store, llm)
    await engine.record_episode("s1", "user", "我喜欢喝绿茶")
    assert await ProfileStore(store).list_all()


async def test_only_user_turns_trigger_the_extraction(store):
    llm = FakeLLM(json.dumps([{"key": "user.role", "value": "律师"}]))
    engine = make_engine(store, llm)
    await engine.record_episode("s1", "assistant", "我是律师")
    assert llm.tasks == []


# --------------------------------------------------------------------------
# the pattern-matching path
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase,prefix",
    [
        ("我喜欢", "user.preference.like"),
        ("我更喜欢", "user.preference.prefer"),
        ("我讨厌", "user.preference.dislike"),
        ("我不喜欢", "user.preference.dislike"),
    ],
)
async def test_the_patterns_are_recognised(store, phrase: str, prefix: str):
    engine = make_engine(store, None)
    await engine.record_episode("s1", "user", f"{phrase}喝茶")
    entries = await ProfileStore(store).list_all()
    assert len(entries) == 1
    assert entries[0]["key"].startswith(prefix)
    assert entries[0]["confidence"] == 0.6


async def test_only_the_first_pattern_is_used(store):
    engine = make_engine(store, None)
    await engine.record_episode("s1", "user", "我喜欢喝茶，我讨厌咖啡")
    entries = await ProfileStore(store).list_all()
    assert len(entries) == 1
    assert entries[0]["key"].startswith("user.preference.like")


async def test_the_pattern_path_has_no_length_or_sensitive_filter(store):
    """The fallback writes long and sensitive values as they are."""
    engine = make_engine(store, None)
    await engine.record_episode("s1", "user", "我喜欢用身份证号110101199001011234")
    entries = await ProfileStore(store).list_all()
    assert len(entries) == 1
    assert "身份证" in entries[0]["value"]
    assert len(entries[0]["value"]) > 20


async def test_a_turn_without_a_pattern_writes_nothing(store):
    engine = make_engine(store, None)
    await engine.record_episode("s1", "user", "今天天气不错")
    assert await ProfileStore(store).list_all() == []
