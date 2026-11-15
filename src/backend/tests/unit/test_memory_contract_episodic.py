"""The episodic memory: what is written and what can be read back.

* the fields of a conversation record
* the assistant record's preconditions
* the vector column, which is never filled
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.episodic import EpisodicMemory


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def memory(store) -> EpisodicMemory:
    return EpisodicMemory(store)


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


async def test_an_episode_round_trips(memory, store):
    episode_id = await memory.add(
        session_id="s1", role="user", content="合同里的违约金是多少",
        topic_tags=["合同"], entities=["违约金"],
    )
    assert isinstance(episode_id, int)

    async with store.conn.execute("SELECT * FROM episodes") as cur:
        rows = await cur.fetchall()
    from yuanwork.db.sqlite import _row_to_dict

    row = _row_to_dict(rows[0])
    assert row["session_id"] == "s1"
    assert row["role"] == "user"
    assert row["content"] == "合同里的违约金是多少"
    assert row["topic_tags"] == ["合同"]
    assert row["entities"] == ["违约金"]
    assert row["importance_score"] == 0.5


async def test_the_role_is_constrained(memory, store):
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        await store.conn.execute(
            "INSERT INTO episodes (session_id, role, content) VALUES ('s', 'robot', 'x')"
        )


async def test_the_vector_column_is_never_filled(memory, store):
    """Nobody passes an embedding, so the column stays null."""
    await memory.add(session_id="s1", role="user", content="内容")
    async with store.conn.execute("SELECT embedding FROM episodes") as cur:
        rows = await cur.fetchall()
    assert rows[0][0] is None


async def test_the_importance_score_defaults_to_a_half(memory, store):
    await memory.add(session_id="s1", role="user", content="内容")
    async with store.conn.execute("SELECT importance_score FROM episodes") as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == 0.5


async def test_the_same_content_can_be_written_twice(memory, store):
    """There is no idempotency key — a replayed turn adds a second row."""
    await memory.add(session_id="s1", role="user", content="内容")
    await memory.add(session_id="s1", role="user", content="内容")
    assert await memory.count() == 2


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


async def test_the_count_reflects_what_was_written(memory):
    assert await memory.count() == 0
    await memory.add(session_id="s1", role="user", content="一")
    await memory.add(session_id="s1", role="assistant", content="二")
    assert await memory.count() == 2


async def test_the_recent_list_is_newest_first(memory, store):
    await memory.add(session_id="s1", role="user", content="一")
    await memory.add(session_id="s1", role="assistant", content="二")
    # the timestamps have a one-second resolution, so the order inside
    # one second is undefined — age the first row explicitly
    await store.conn.execute(
        "UPDATE episodes SET created_at = '2000-01-01 00:00:00' WHERE content = '一'"
    )
    await store.conn.commit()

    recent = await memory.list_recent(limit=10)
    assert len(recent) == 2
    assert recent[0]["content"] == "二"


async def test_the_recent_list_honours_the_limit(memory):
    for i in range(5):
        await memory.add(session_id="s1", role="user", content=f"内容{i}")
    assert len(await memory.list_recent(limit=2)) == 2


async def test_a_topic_search_matches_an_ascii_tag(memory):
    await memory.add(session_id="s1", role="user", content="无关内容",
                     topic_tags=["contract"])
    found = await memory.search_by_topic("contract", limit=10)
    assert len(found) == 1


async def test_a_chinese_tag_is_stored_escaped_and_cannot_be_found(memory, store):
    """`json.dumps` escapes non-ASCII by default, so the tag is stored as
    `合同` and a LIKE match on the Chinese text misses it."""
    await memory.add(session_id="s1", role="user", content="无关内容",
                     topic_tags=["合同"])
    async with store.conn.execute("SELECT topic_tags FROM episodes") as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == '["\\u5408\\u540c"]'
    assert chr(92) + "u" in rows[0][0]
    assert await memory.search_by_topic("合同", limit=10) == []


async def test_a_topic_search_matches_the_content(memory):
    await memory.add(session_id="s1", role="user", content="违约金条款")
    found = await memory.search_by_topic("违约金", limit=10)
    assert len(found) == 1


async def test_a_topic_search_is_a_substring_match(memory):
    await memory.add(session_id="s1", role="user", content="内容")
    assert await memory.search_by_topic("内容", limit=10)
    assert await memory.search_by_topic("没有这个词", limit=10) == []


async def test_the_episodes_can_be_listed_by_session(memory):
    await memory.add(session_id="s1", role="user", content="一")
    await memory.add(session_id="s2", role="user", content="二")
    found = await memory.list_by_session("s1")
    assert len(found) == 1
    assert found[0]["session_id"] == "s1"


async def test_the_context_window_is_a_slice(memory):
    for i in range(5):
        await memory.add(session_id="s1", role="user", content=f"内容{i}")
    window = await memory.get_context_window("s1", max_turns=2)
    assert len(window) == 2


# --------------------------------------------------------------------------
# the unimplemented parts
# --------------------------------------------------------------------------


async def test_deleting_a_session_is_not_implemented(memory):
    with pytest.raises(NotImplementedError):
        await memory.delete_session("s1")


async def test_the_context_window_has_no_production_caller():
    """`get_context_window` and `list_by_session` exist but nothing in
    the codebase calls them."""
    import inspect

    from yuanwork.memory.episodic import EpisodicMemory
    from yuanwork.orchestrator import agent

    agent_source = inspect.getsource(agent)
    assert "get_context_window" not in agent_source
    assert "list_by_session" not in agent_source
    assert hasattr(EpisodicMemory, "get_context_window")
