"""Tests for SQLite store."""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.db.sqlite import SQLiteStore


@pytest.mark.asyncio
async def test_initialize(temp_dir: Path):
    """SQLiteStore initializes schema."""
    db_path = temp_dir / "test.db"
    store = SQLiteStore(db_path)
    await store.initialize()

    # Tables exist
    async with store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cursor:
        rows = await cursor.fetchall()
    table_names = {r[0] for r in rows}

    assert "files" in table_names
    assert "episodes" in table_names
    assert "user_profile" in table_names
    assert "wiki_pages" in table_names
    assert "wiki_links" in table_names
    assert "entities" in table_names
    assert "conflicts" in table_names

    await store.close()


@pytest.mark.asyncio
async def test_file_upsert(temp_dir: Path):
    """File records can be inserted and retrieved."""
    db_path = temp_dir / "test.db"
    store = SQLiteStore(db_path)
    await store.initialize()

    # Insert
    await store.upsert_file(
        doc_id="abc123",
        file_name="contract.pdf",
        file_path="/tmp/contract.pdf",
        file_hash="deadbeef",
        file_type="pdf",
        size_bytes=1024,
        chunk_count=5,
    )

    # Retrieve
    file_info = await store.get_file("abc123")
    assert file_info is not None
    assert file_info["file_name"] == "contract.pdf"
    assert file_info["chunk_count"] == 5

    # Upsert (update)
    await store.upsert_file(
        doc_id="abc123",
        file_name="contract.pdf",
        file_path="/tmp/contract.pdf",
        file_hash="deadbeef",
        file_type="pdf",
        size_bytes=1024,
        chunk_count=10,  # Updated
    )
    file_info = await store.get_file("abc123")
    assert file_info["chunk_count"] == 10

    await store.close()


@pytest.mark.asyncio
async def test_episode_recording(temp_dir: Path):
    """Episodes can be added and retrieved."""
    db_path = temp_dir / "test.db"
    store = SQLiteStore(db_path)
    await store.initialize()

    ep_id = await store.add_episode(
        session_id="session-1",
        role="user",
        content="我最近在研究 AI 记忆系统",
        topic_tags=["AI", "memory"],
        entities=["记忆系统"],
    )
    assert ep_id > 0

    recent = await store.list_recent_episodes(limit=10)
    assert len(recent) == 1
    assert recent[0]["content"] == "我最近在研究 AI 记忆系统"
    assert "AI" in recent[0]["topic_tags"]

    await store.close()


@pytest.mark.asyncio
async def test_wiki_page_lifecycle(temp_dir: Path):
    """Wiki pages can be created and retrieved."""
    db_path = temp_dir / "test.db"
    store = SQLiteStore(db_path)
    await store.initialize()

    # Create page
    await store.upsert_wiki_page(
        page_id="购房合同",
        title="购房合同违约金",
        markdown="# 购房合同违约金\n\n测试内容",
        summary="关于购房合同违约金的总结",
        category="contract",
        tags=["购房", "违约金"],
    )

    # Get
    page = await store.get_wiki_page("购房合同")
    assert page is not None
    assert page["title"] == "购房合同违约金"
    assert page["category"] == "contract"

    # Update
    await store.upsert_wiki_page(
        page_id="购房合同",
        title="购房合同违约金（更新）",
        markdown="# 更新内容",
        summary="更新",
        category="contract",
    )
    page = await store.get_wiki_page("购房合同")
    assert page["title"] == "购房合同违约金（更新）"
    assert page["version"] == 2  # Incremented

    # Delete
    await store.delete_wiki_page("购房合同")
    page = await store.get_wiki_page("购房合同")
    assert page is None

    await store.close()


@pytest.mark.asyncio
async def test_wiki_links(temp_dir: Path):
    """Wiki links can be created and retrieved as backlinks."""
    db_path = temp_dir / "test.db"
    store = SQLiteStore(db_path)
    await store.initialize()

    # Create pages
    await store.upsert_wiki_page(
        page_id="page-a", title="Page A", markdown="", summary="A"
    )
    await store.upsert_wiki_page(
        page_id="page-b", title="Page B", markdown="", summary="B"
    )

    # Add link
    await store.add_wiki_link(
        source_id="page-a",
        source_type="page",
        target_id="page-b",
        target_type="page",
        link_type="related",
    )

    # Get backlinks for page-b
    backlinks = await store.get_wiki_backlinks("page-b")
    assert len(backlinks) == 1
    assert backlinks[0]["source_id"] == "page-a"
    assert backlinks[0]["target_id"] == "page-b"

    await store.close()


@pytest.mark.asyncio
async def test_profile(temp_dir: Path):
    """Profile entries can be set and retrieved."""
    db_path = temp_dir / "test.db"
    store = SQLiteStore(db_path)
    await store.initialize()

    await store.upsert_profile(
        key="user.preference.like.辣",
        value="我很喜欢吃辣",
        confidence=0.8,
    )

    profile = await store.get_profile("user.preference.like.辣")
    assert profile is not None
    assert profile["value"] == "我很喜欢吃辣"
    assert profile["confidence"] == 0.8

    profiles = await store.list_profile(prefix="user.")
    assert len(profiles) == 1

    await store.close()


@pytest.mark.asyncio
async def test_conflict_logging(temp_dir: Path):
    """Conflicts can be logged and listed."""
    db_path = temp_dir / "test.db"
    store = SQLiteStore(db_path)
    await store.initialize()

    conflict_id = await store.add_conflict(
        conflict_type="llm_vs_kb",
        llm_answer="Python dataclass 用 @dataclass(eq=True)",
        kb_evidence="过时写法",
        knowledge_chunk_id="chunk-123",
    )
    assert conflict_id > 0

    conflicts = await store.list_conflicts(include_acknowledged=False)
    assert len(conflicts) == 1
    assert conflicts[0]["conflict_type"] == "llm_vs_kb"

    # Acknowledge
    await store.acknowledge_conflict(conflict_id)
    unacked = await store.list_conflicts(include_acknowledged=False)
    assert len(unacked) == 0

    await store.close()
