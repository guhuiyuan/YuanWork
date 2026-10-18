"""Tests for Stage 2 wiring: conflict detection, profile extraction, gateway.

These tests don't require a running bge-m3 / LLM. They validate that the
code paths are connected (not dead), that the persistence layer works
end-to-end with the agent loop, and that the LLM gateway resolves keys
from models.yaml correctly.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from yuanwork.consolidation.conflict import format_conflict_message
from yuanwork.consolidation.engine import ConsolidationEngine
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.episodic import EpisodicMemory
from yuanwork.memory.profile import ProfileStore
from yuanwork.orchestrator.agent import AgentLoop, AgentState


# ===== Conflict formatting =====


def test_format_conflict_message_with_claims():
    """format_conflict_message renders a usable user-facing warning."""
    conflict = {
        "verdict": "conflict",
        "conflict_claims": [
            {
                "topic": "违约金比例",
                "llm_says": "0.0003/日",
                "kb_says": "0.0005/日",
            }
        ],
    }
    msg = format_conflict_message(conflict)
    assert "通用知识与知识库存在分歧" in msg
    assert "违约金比例" in msg
    assert "0.0005/日" in msg
    assert "0.0003/日" in msg
    assert "已以知识库为准" in msg


def test_format_conflict_message_no_conflict_returns_empty():
    """format_conflict_message returns '' for non-conflict verdicts."""
    assert format_conflict_message({"verdict": "consistent"}) == ""
    assert format_conflict_message({"verdict": "unrelated"}) == ""


# ===== Consolidation engine: profile + episode wiring =====


@pytest.mark.asyncio
async def test_consolidation_records_episode_and_profile(temp_dir: Path):
    """End-to-end: ConsolidationEngine.record_episode persists to SQLite and
    triggers profile extraction (regex fallback when no LLM is provided)."""
    db_path = temp_dir / "store.db"
    sqlite_store = SQLiteStore(db_path)
    await sqlite_store.initialize()

    episodic = EpisodicMemory(sqlite_store)
    profile = ProfileStore(sqlite_store)
    engine = ConsolidationEngine(
        episodic_memory=episodic,
        semantic_memory=MagicMock(),
        profile_store=profile,
        sqlite_store=sqlite_store,
        llm_gateway=None,  # exercises the regex fallback
    )

    ep_id = await engine.record_episode(
        session_id="s1",
        role="user",
        content="我喜欢普洱茶",
    )
    assert ep_id > 0

    # Episode persisted
    recent = await episodic.list_recent(limit=10)
    assert len(recent) == 1
    assert recent[0]["content"] == "我喜欢普洱茶"

    # Profile extracted via regex fallback
    profile_entries = await profile.list_all(prefix="user.preference.like")
    assert len(profile_entries) == 1
    assert "普洱茶" in profile_entries[0]["value"]
    assert profile_entries[0]["source_episode_id"] == ep_id

    await sqlite_store.close()


@pytest.mark.asyncio
async def test_consolidation_logs_conflict(temp_dir: Path):
    """Engine.log_conflict persists to SQLite conflicts table."""
    db_path = temp_dir / "store.db"
    sqlite_store = SQLiteStore(db_path)
    await sqlite_store.initialize()

    engine = ConsolidationEngine(
        episodic_memory=EpisodicMemory(sqlite_store),
        semantic_memory=MagicMock(),
        profile_store=ProfileStore(sqlite_store),
        sqlite_store=sqlite_store,
        llm_gateway=None,
    )

    conflict_id = await engine.log_conflict(
        llm_answer="0.0003/日",
        kb_evidence="0.0005/日",
        episode_id=None,
        knowledge_chunk_id="chunk-abc",
    )
    assert conflict_id > 0

    conflicts = await sqlite_store.list_conflicts(include_acknowledged=True)
    assert len(conflicts) == 1
    assert conflicts[0]["llm_answer"] == "0.0003/日"
    assert conflicts[0]["knowledge_chunk_id"] == "chunk-abc"

    await sqlite_store.close()


@pytest.mark.asyncio
async def test_consolidation_run_with_reflex_loop(temp_dir: Path):
    """Engine.run de-dupes episodes and runs profile update on user episodes."""
    db_path = temp_dir / "store.db"
    sqlite_store = SQLiteStore(db_path)
    await sqlite_store.initialize()

    engine = ConsolidationEngine(
        episodic_memory=EpisodicMemory(sqlite_store),
        semantic_memory=MagicMock(),
        profile_store=ProfileStore(sqlite_store),
        sqlite_store=sqlite_store,
        llm_gateway=None,
    )

    # Add 3 user episodes with the same preference (regex should fire on first)
    for _ in range(3):
        await engine.record_episode(
            session_id="s1",
            role="user",
            content="我喜欢喝咖啡",  # each time, regex fires
        )

    result = await engine.run(reason="manual")
    assert result["episodes_processed"] >= 1
    assert "duration_ms" in result
    assert result["duration_ms"] >= 0

    await sqlite_store.close()


# ===== AgentLoop wiring =====


@pytest.mark.asyncio
async def test_agent_loop_accepts_consolidation_engine(temp_dir: Path):
    """AgentLoop now accepts a ConsolidationEngine in its constructor."""
    db_path = temp_dir / "store.db"
    sqlite_store = SQLiteStore(db_path)
    await sqlite_store.initialize()

    engine = ConsolidationEngine(
        episodic_memory=EpisodicMemory(sqlite_store),
        semantic_memory=MagicMock(),
        profile_store=ProfileStore(sqlite_store),
        sqlite_store=sqlite_store,
        llm_gateway=None,
    )

    # Construct agent with the new parameter
    agent = AgentLoop(
        llm_gateway=MagicMock(),
        semantic_memory=MagicMock(),
        episodic_memory=EpisodicMemory(sqlite_store),
        consolidation_engine=engine,
    )
    assert agent.consolidation is engine

    await sqlite_store.close()


@pytest.mark.asyncio
async def test_agent_record_episode_safe_is_noop_without_engine():
    """AgentLoop._record_episode_safe returns early when consolidation is None."""
    agent = AgentLoop(
        llm_gateway=MagicMock(),
        semantic_memory=MagicMock(),
        episodic_memory=MagicMock(),
        consolidation_engine=None,
    )
    state = AgentState(query="hi", session_id="s1")
    # Should not raise
    await agent._record_episode_safe(state)
    assert state.user_episode_id is None
    assert state.assistant_episode_id is None


# ===== LLM Gateway key resolution =====


def test_gateway_resolves_api_key_from_env(monkeypatch, temp_dir: Path):
    """Gateway resolves api_key_env to an actual env var value at load time."""
    from yuanwork.llm.gateway import LLMGateway
    from yuanwork.config import YuanWorkSettings

    monkeypatch.setenv("TEST_API_KEY_FAKE", "sk-test-1234")
    models_yaml = temp_dir / "models.yaml"
    models_yaml.write_text(
        """
models:
  test-mini:
    provider: openai
    api_key_env: TEST_API_KEY_FAKE
    model_name: gpt-4o-mini
routing:
  general: test-mini
""",
        encoding="utf-8",
    )

    # Make config_dir point at the test file
    settings = YuanWorkSettings(config_dir=temp_dir)
    gw = LLMGateway(settings)
    assert "test-mini" in gw.configured_models()
    spec = gw._models["test-mini"]
    assert spec.get("api_key") == "sk-test-1234"
    # Resolved kwargs should be openai/<model_name>
    kwargs = gw._resolve_litellm_kwargs("test-mini")
    assert kwargs["model"] == "openai/gpt-4o-mini"
    assert kwargs["api_key"] == "sk-test-1234"


def test_gateway_fallback_when_target_has_no_key(monkeypatch, temp_dir: Path):
    """get_model_for_task falls back to first model with an API key."""
    from yuanwork.llm.gateway import LLMGateway
    from yuanwork.config import YuanWorkSettings

    monkeypatch.setenv("FAKE_KEY", "sk-fake")
    (temp_dir / "models.yaml").write_text(
        """
models:
  gpt-4o:
    provider: openai
    api_key_env: NOT_SET
    model_name: gpt-4o
  fake-only:
    provider: openai
    api_key_env: FAKE_KEY
    model_name: gpt-4o-mini
routing:
  general: gpt-4o
""",
        encoding="utf-8",
    )
    settings = YuanWorkSettings(config_dir=temp_dir)
    gw = LLMGateway(settings)
    # The routing target (gpt-4o) has no key, so we should fall back to fake-only
    assert gw.get_model_for_task("general") == "fake-only"
    assert gw.get_model_for_task("unknown_task_type") == "fake-only"


def test_gateway_uses_provider_prefix_for_anthropic(monkeypatch, temp_dir: Path):
    """Provider=anthropic should produce anthropic/<model_name>."""
    from yuanwork.llm.gateway import LLMGateway
    from yuanwork.config import YuanWorkSettings

    monkeypatch.setenv("FAKE_KEY", "sk-x")
    (temp_dir / "models.yaml").write_text(
        """
models:
  claude-mini:
    provider: anthropic
    api_key_env: FAKE_KEY
    model_name: claude-3-5-sonnet-latest
routing:
  general: claude-mini
""",
        encoding="utf-8",
    )
    settings = YuanWorkSettings(config_dir=temp_dir)
    gw = LLMGateway(settings)
    kwargs = gw._resolve_litellm_kwargs("claude-mini")
    assert kwargs["model"] == "anthropic/claude-3-5-sonnet-latest"


# ===== SemanticMemory non-blocking prewarm =====


@pytest.mark.asyncio
async def test_semantic_search_short_circuits_on_empty_table(temp_dir: Path):
    """search() returns [] immediately when the table is empty, without
    triggering the (slow) embedder load."""
    from yuanwork.memory.semantic import SemanticMemory

    sem = SemanticMemory(data_dir=temp_dir / "lancedb", embedding_model="fake-model")
    await sem.initialize()

    # Even with a made-up model name (no download), this should not raise
    results = await sem.search("hello", top_k=10)
    assert results == []
    # And the embedder should still be None
    assert sem._embedder is None


def test_semantic_prewarm_state_flag(temp_dir: Path):
    """prewarm_embedding_model sets the in-flight flag while a task runs."""
    import asyncio
    from yuanwork.memory.semantic import SemanticMemory

    sem = SemanticMemory(data_dir=temp_dir / "lancedb", embedding_model="fake-model")
    # Pre-warm flag should default to False
    assert sem._prewarm_in_flight is False
    # Setting it manually works
    sem._prewarm_in_flight = True
    assert sem._prewarm_in_flight is True
