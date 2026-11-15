"""The Wiki compile trigger: cooldown and serialisation.

* the same topic is compiled once per cooldown window
* two concurrent compiles of the same topic are serialised
* a failed compile is reported, not raised
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from yuanwork.wiki.trigger import WikiCompileTrigger


class FakeGenerator:
    """Stands in for the page generator."""

    def __init__(self, fail: bool = False, delay: float = 0.0):
        self.fail = fail
        self.delay = delay
        self.calls: list[dict] = []
        self.concurrent = 0
        self.max_concurrent = 0

    async def compile_page(self, title, chunks, category="general", page_id=None):
        self.calls.append({"title": title, "page_id": page_id, "category": category})
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.concurrent -= 1
        if self.fail:
            raise RuntimeError("生成失败")
        return {"page_id": page_id, "title": title, "markdown": "# 页面"}


@pytest.fixture
def generator() -> FakeGenerator:
    return FakeGenerator()


@pytest.fixture
def trigger(generator) -> WikiCompileTrigger:
    t = WikiCompileTrigger(sqlite_store=None, llm_gateway=None)  # type: ignore[arg-type]
    t._generator = generator  # type: ignore[attr-defined]
    return t


def evidence(count: int = 5) -> list[dict]:
    return [
        {"id": f"c{i}", "doc_id": f"d{i % 2}", "text": f"片段 {i}", "_score": 0.8}
        for i in range(count)
    ]


UNDERSTANDING = {"intent": "contract", "topic": "违约金条款"}


# --------------------------------------------------------------------------
# the cooldown
# --------------------------------------------------------------------------


async def test_a_topic_is_compiled_once(trigger, generator):
    result = await trigger.maybe_compile(evidence(), UNDERSTANDING)
    assert result["triggered"] is True
    assert result["reason"] == "ok"
    assert result["page_id"] == "auto/" + trigger.extract_topic(UNDERSTANDING)
    assert len(generator.calls) == 1


async def test_the_same_topic_is_throttled_afterwards(trigger, generator):
    await trigger.maybe_compile(evidence(), UNDERSTANDING)
    second = await trigger.maybe_compile(evidence(), UNDERSTANDING)
    assert second["triggered"] is False
    assert second["reason"] == "throttled"
    assert len(generator.calls) == 1


async def test_a_different_topic_is_not_throttled(trigger, generator):
    await trigger.maybe_compile(evidence(), UNDERSTANDING)
    await trigger.maybe_compile(
        evidence(), {"intent": "contract", "topic": "另一条款"}
    )
    assert len(generator.calls) == 2


async def test_the_cooldown_can_be_shortened(trigger, generator):
    trigger.cooldown_seconds = 0.0
    await trigger.maybe_compile(evidence(), UNDERSTANDING)
    await trigger.maybe_compile(evidence(), UNDERSTANDING)
    assert len(generator.calls) == 2


async def test_the_cooldown_state_is_in_process_only(trigger, generator):
    """The cooldown is a plain dict — a restart forgets it."""
    await trigger.maybe_compile(evidence(), UNDERSTANDING)
    assert trigger._last_compile

    fresh = WikiCompileTrigger(sqlite_store=None, llm_gateway=None)  # type: ignore[arg-type]
    fresh._generator = generator  # type: ignore[attr-defined]
    await fresh.maybe_compile(evidence(), UNDERSTANDING)
    assert len(generator.calls) == 2


# --------------------------------------------------------------------------
# the serialisation
# --------------------------------------------------------------------------


async def test_two_compiles_of_the_same_topic_are_serialised(generator):
    trigger = WikiCompileTrigger(sqlite_store=None, llm_gateway=None)  # type: ignore[arg-type]
    generator.delay = 0.05
    trigger._generator = generator  # type: ignore[attr-defined]

    await asyncio.gather(
        trigger.maybe_compile(evidence(), UNDERSTANDING),
        trigger.maybe_compile(evidence(), UNDERSTANDING),
    )
    # the second one waited for the first, then found the cooldown
    assert generator.max_concurrent == 1


async def test_different_topics_may_compile_in_parallel(generator):
    trigger = WikiCompileTrigger(sqlite_store=None, llm_gateway=None)  # type: ignore[arg-type]
    generator.delay = 0.05
    trigger._generator = generator  # type: ignore[attr-defined]

    await asyncio.gather(
        trigger.maybe_compile(evidence(), {"intent": "a", "topic": "甲"}),
        trigger.maybe_compile(evidence(), {"intent": "b", "topic": "乙"}),
    )
    assert generator.max_concurrent == 2


async def test_the_lock_is_kept_per_topic(trigger):
    await trigger._get_topic_lock("甲")
    await trigger._get_topic_lock("甲")
    assert len(trigger._topic_locks) == 1


# --------------------------------------------------------------------------
# the failures
# --------------------------------------------------------------------------


async def test_a_failing_compile_is_reported_not_raised(generator):
    generator.fail = True
    trigger = WikiCompileTrigger(sqlite_store=None, llm_gateway=None)  # type: ignore[arg-type]
    trigger._generator = generator  # type: ignore[attr-defined]

    result = await trigger.maybe_compile(evidence(), UNDERSTANDING)
    assert result["triggered"] is False
    assert result["error"]
    assert "生成失败" in result["error"]


async def test_a_failed_compile_does_not_start_the_cooldown(generator):
    generator.fail = True
    trigger = WikiCompileTrigger(sqlite_store=None, llm_gateway=None)  # type: ignore[arg-type]
    trigger._generator = generator  # type: ignore[attr-defined]

    await trigger.maybe_compile(evidence(), UNDERSTANDING)
    assert trigger._last_compile == {}


async def test_the_result_carries_the_five_fields(trigger):
    result = await trigger.maybe_compile(evidence(), UNDERSTANDING)
    assert set(result) == {"triggered", "reason", "page_id", "title", "error"}
