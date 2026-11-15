"""Unit tests for the Wiki Page proactive compile trigger (Stage 2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.wiki.page_generator import WikiPageGenerator
from yuanwork.wiki.trigger import (
    GENERIC_INTENTS,
    WikiCompileTrigger,
)


# ===== Fixtures =====


@pytest_asyncio.fixture
async def sqlite_store(tmp_path: Path):
    """Fresh in-memory-ish SQLite store for each test."""
    store = SQLiteStore(tmp_path / "test.db")
    await store.initialize()
    yield store
    await store.close()


def _evidence_chunk(doc_id: str, score: float, text: str = "x") -> dict:
    """Build an evidence chunk dict matching the orchestrator's output shape."""
    return {
        "id": f"{doc_id}-c1",
        "doc_id": doc_id,
        "text": text,
        "file_name": f"{doc_id}.md",
        "_score": score,
    }


def _understanding(topic: str = "违约金条款", intent: str = "contract") -> dict:
    return {
        "intent": intent,
        "topic": topic,
        "file_hint": None,
        "time_hint": None,
        "entity_hints": [],
    }


# ===== should_trigger tests =====


def test_should_trigger_returns_false_on_empty_evidence():
    t = WikiCompileTrigger(sqlite_store=MagicMock(), llm_gateway=None)
    should, reason = t.should_trigger([], _understanding())
    assert should is False
    assert reason == "no_evidence"


def test_should_trigger_returns_false_on_too_few_chunks():
    t = WikiCompileTrigger(sqlite_store=MagicMock(), llm_gateway=None)
    ev = [_evidence_chunk("a", 0.9), _evidence_chunk("a", 0.8)]
    should, reason = t.should_trigger(ev, _understanding())
    assert should is False
    assert reason == "too_few_chunks"


def test_should_trigger_returns_false_on_single_doc():
    t = WikiCompileTrigger(sqlite_store=MagicMock(), llm_gateway=None)
    ev = [
        _evidence_chunk("a", 0.9),
        _evidence_chunk("a", 0.8),
        _evidence_chunk("a", 0.7),
        _evidence_chunk("a", 0.6),
    ]
    should, reason = t.should_trigger(ev, _understanding())
    assert should is False
    assert reason == "single_doc"


def test_should_trigger_returns_false_on_low_top_score():
    t = WikiCompileTrigger(sqlite_store=MagicMock(), llm_gateway=None)
    ev = [
        _evidence_chunk("a", 0.4),
        _evidence_chunk("b", 0.3),
        _evidence_chunk("c", 0.2),
        _evidence_chunk("d", 0.1),
    ]
    should, reason = t.should_trigger(ev, _understanding())
    assert should is False
    assert reason == "low_top_score"


def test_should_trigger_returns_false_without_topic():
    t = WikiCompileTrigger(sqlite_store=MagicMock(), llm_gateway=None)
    ev = [
        _evidence_chunk("a", 0.9),
        _evidence_chunk("b", 0.8),
        _evidence_chunk("c", 0.7),
        _evidence_chunk("d", 0.6),
    ]
    should, reason = t.should_trigger(ev, {"intent": "contract", "topic": ""})
    assert should is False
    assert reason == "no_topic"


def test_should_trigger_returns_false_on_generic_intent():
    t = WikiCompileTrigger(sqlite_store=MagicMock(), llm_gateway=None)
    ev = [
        _evidence_chunk("a", 0.9),
        _evidence_chunk("b", 0.8),
        _evidence_chunk("c", 0.7),
        _evidence_chunk("d", 0.6),
    ]
    for intent in GENERIC_INTENTS:
        should, reason = t.should_trigger(ev, _understanding(intent=intent))
        assert should is False, f"intent={intent!r} should not trigger"
        assert reason == "generic_intent"


def test_should_trigger_returns_true_on_all_conditions_met():
    t = WikiCompileTrigger(sqlite_store=MagicMock(), llm_gateway=None)
    ev = [
        _evidence_chunk("a", 0.9),
        _evidence_chunk("b", 0.8),
        _evidence_chunk("c", 0.7),
        _evidence_chunk("d", 0.6),
    ]
    should, reason = t.should_trigger(ev, _understanding())
    assert should is True
    assert reason == "ok"


# ===== Throttle tests =====


def test_throttle_blocks_repeat_within_cooldown():
    t = WikiCompileTrigger(
        sqlite_store=MagicMock(), llm_gateway=None, cooldown_seconds=300
    )
    topic = _understanding()["topic"]
    t._last_compile[topic] = 999_999_999  # far in the future

    ev = [
        _evidence_chunk("a", 0.9),
        _evidence_chunk("b", 0.8),
        _evidence_chunk("c", 0.7),
        _evidence_chunk("d", 0.6),
    ]
    should, reason = t.should_trigger(ev, _understanding())
    assert should is False
    assert reason == "throttled"


# ===== maybe_compile end-to-end (no LLM) =====


@pytest.mark.asyncio
async def test_maybe_compile_uses_fallback_when_no_llm(sqlite_store):
    """Without an LLM, the page generator falls back to a non-LLM template."""
    t = WikiCompileTrigger(sqlite_store=sqlite_store, llm_gateway=None)
    ev = [
        _evidence_chunk("a", 0.9, "这是来自文档 A 的内容"),
        _evidence_chunk("b", 0.8, "这是来自文档 B 的内容"),
        _evidence_chunk("c", 0.7, "这是来自文档 C 的内容"),
        _evidence_chunk("d", 0.6, "这是来自文档 D 的内容"),
    ]
    result = await t.maybe_compile(ev, _understanding())
    assert result["triggered"] is True
    assert result["page_id"] == "auto/" + t.extract_topic(_understanding())
    assert result["error"] is None
    assert result["title"] == "违约金条款"

    # Verify the page is in SQLite
    page = await sqlite_store.get_wiki_page(result["page_id"])
    assert page is not None
    assert page["title"] == "违约金条款"

    # Verify chunks are linked to the page (outgoing: page → chunks)
    outgoing = await sqlite_store.get_wiki_outgoing_links(result["page_id"])
    chunk_links = [l for l in outgoing if l["target_type"] == "chunk"]
    assert len(chunk_links) == 4  # one per evidence chunk
    # And the page points at the documents it came from, too.
    doc_links = [l for l in outgoing if l["target_type"] == "doc"]
    assert len(doc_links) == 4


@pytest.mark.asyncio
async def test_maybe_compile_skips_when_should_not_trigger(sqlite_store):
    t = WikiCompileTrigger(sqlite_store=sqlite_store, llm_gateway=None)
    result = await t.maybe_compile([], _understanding())
    assert result["triggered"] is False
    assert result["reason"] == "no_evidence"
    assert result["page_id"] is None

    # And nothing was written
    count = await sqlite_store.count_wiki_pages()
    assert count == 0


@pytest.mark.asyncio
async def test_maybe_compile_handles_compile_error_gracefully(sqlite_store):
    """If the underlying generator raises, maybe_compile must not raise."""

    class _BoomGenerator:
        async def compile_page(self, **_kwargs):
            raise RuntimeError("LLM timeout")

    t = WikiCompileTrigger(sqlite_store=sqlite_store, llm_gateway=None)
    t._generator = _BoomGenerator()
    ev = [
        _evidence_chunk("a", 0.9),
        _evidence_chunk("b", 0.8),
        _evidence_chunk("c", 0.7),
        _evidence_chunk("d", 0.6),
    ]
    result = await t.maybe_compile(ev, _understanding())
    assert result["triggered"] is False
    assert "LLM timeout" in result["error"]


@pytest.mark.asyncio
async def test_maybe_compile_throttles_repeat_topic(sqlite_store):
    t = WikiCompileTrigger(
        sqlite_store=sqlite_store, llm_gateway=None, cooldown_seconds=300
    )
    ev = [
        _evidence_chunk("a", 0.9),
        _evidence_chunk("b", 0.8),
        _evidence_chunk("c", 0.7),
        _evidence_chunk("d", 0.6),
    ]
    first = await t.maybe_compile(ev, _understanding())
    assert first["triggered"] is True

    # Same topic, immediately again: throttled (cheap check before lock)
    second = await t.maybe_compile(ev, _understanding())
    assert second["triggered"] is False
    assert second["reason"] == "throttled"

    # Different topic: not throttled
    third = await t.maybe_compile(ev, _understanding(topic="Other topic"))
    assert third["triggered"] is True
    assert third["page_id"] != first["page_id"]


# ===== Title / category / slug derivation =====


def test_extract_topic_slugifies_unicode():
    t = WikiCompileTrigger(sqlite_store=MagicMock(), llm_gateway=None)
    assert t.extract_topic({"topic": "违约金条款"}) == "违约金条款"
    # Slug is preserved (Chinese chars pass through \w in Python)
    assert t.extract_topic({"topic": "Python dataclass 性能"}) == "python-dataclass-性能"


def test_derive_title_truncates_long_topics():
    t = WikiCompileTrigger(sqlite_store=MagicMock(), llm_gateway=None)
    long_topic = "a" * 100
    title = t.derive_title({"topic": long_topic})
    assert len(title) <= 64


def test_derive_category_maps_intents():
    t = WikiCompileTrigger(sqlite_store=MagicMock(), llm_gateway=None)
    assert t.derive_category({"intent": "contract"}) == "contract"
    assert t.derive_category({"intent": "law"}) == "law"
    assert t.derive_category({"intent": "project"}) == "project"
    assert t.derive_category({"intent": "concept"}) == "concept"
    assert t.derive_category({"intent": "person"}) == "person"
    assert t.derive_category({"intent": "general"}) == "general"
    assert t.derive_category({}) == "general"


def test_wiki_prompt_format_does_not_raise():
    """Regression: the WIKI_PAGE_PROMPT used to have Chinese example
    placeholders ({标题}, {category}, etc.) that .format() consumed as
    format fields, causing KeyError at runtime. The fix escapes them as
    {{...}} so they're literal in the prompt."""
    from yuanwork.wiki.page_generator import WIKI_PAGE_PROMPT

    # This must not raise KeyError on the example placeholders.
    rendered = WIKI_PAGE_PROMPT.format(
        title="违约金条款",
        chunks_text="片段内容",
        date_placeholder="2026-10-18",
    )
    # The real placeholders are substituted.
    assert "片段内容" in rendered
    # The escaped example placeholders appear as literal braces in the prompt.
    # (The LLM sees these and is expected to copy them; the post-process
    # step in compile_page() then replaces {date_placeholder} with the
    # real date in the final stored markdown.)
    assert "{标题}" in rendered
    assert "{category}" in rendered
    assert "{要点 1}" in rendered
    assert "{date_placeholder}" in rendered


@pytest.mark.asyncio
async def test_compile_page_replaces_date_placeholder_in_output(sqlite_store):
    """The LLM copies {date_placeholder} literally from the example format.
    The page generator must substitute it before saving."""
    # Use the no-LLM fallback path so we control the output exactly.
    gen = WikiPageGenerator(sqlite_store=sqlite_store, llm_gateway=None)
    chunks = [_evidence_chunk("a", 0.9, "占位符 {date_placeholder} 测试")]
    result = await gen.compile_page(
        title="日期占位符测试",
        chunks=chunks,
        category="general",
        page_id="test/date-placeholder",
    )
    page = await sqlite_store.get_wiki_page("test/date-placeholder")
    assert page is not None
    # The stored markdown should not contain the raw placeholder.
    assert "{date_placeholder}" not in page["markdown"]


# ===== Imports for tests =====


from unittest.mock import MagicMock  # noqa: E402
