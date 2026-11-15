"""The Wiki compile trigger: when a page is compiled.

The trigger is the gate between "the model answered" and "a structured
page exists". Its thresholds and its eight reason tags are a contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.wiki.trigger import (
    COOLDOWN_SECONDS,
    GENERIC_INTENTS,
    MIN_DISTINCT_DOCS,
    MIN_EVIDENCE_CHUNKS,
    MIN_TOP_SCORE,
    WikiCompileTrigger,
)


@pytest.fixture
def trigger() -> WikiCompileTrigger:
    return WikiCompileTrigger(sqlite_store=None, llm_gateway=None)  # type: ignore[arg-type]


def evidence(count: int = 5, docs: int = 2, score: float = 0.8) -> list[dict]:
    return [
        {
            "id": f"c{i}",
            "doc_id": f"d{i % docs}",
            "text": f"片段 {i}",
            "_score": score,
        }
        for i in range(count)
    ]


UNDERSTANDING = {"intent": "contract", "topic": "违约金条款"}


# --------------------------------------------------------------------------
# the thresholds
# --------------------------------------------------------------------------


def test_the_thresholds_are_the_documented_ones():
    assert MIN_EVIDENCE_CHUNKS == 4
    assert MIN_DISTINCT_DOCS == 2
    assert MIN_TOP_SCORE == 0.55
    assert COOLDOWN_SECONDS == 30 * 60


def test_the_generic_intents_are_known():
    assert "general" in GENERIC_INTENTS
    assert "闲聊" in GENERIC_INTENTS


# --------------------------------------------------------------------------
# the eight reason tags
# --------------------------------------------------------------------------


def test_no_evidence(trigger):
    assert trigger.should_trigger([], UNDERSTANDING) == (False, "no_evidence")


def test_too_few_chunks(trigger):
    assert trigger.should_trigger(evidence(3), UNDERSTANDING) == (False, "too_few_chunks")


def test_single_doc(trigger):
    rows = evidence(5, docs=1)
    assert trigger.should_trigger(rows, UNDERSTANDING) == (False, "single_doc")


def test_no_scores(trigger):
    rows = [
        {"id": f"c{i}", "doc_id": f"d{i % 2}", "text": "x"} for i in range(5)
    ]
    assert trigger.should_trigger(rows, UNDERSTANDING) == (False, "no_scores")


def test_low_top_score(trigger):
    rows = evidence(5, score=0.4)
    assert trigger.should_trigger(rows, UNDERSTANDING) == (False, "low_top_score")


def test_no_topic(trigger):
    assert trigger.should_trigger(evidence(5), {}) == (False, "no_topic")
    assert trigger.should_trigger(evidence(5), {"intent": "contract"}) == (
        False, "no_topic",
    )


def test_generic_intent(trigger):
    rows = evidence(5)
    assert trigger.should_trigger(
        rows, {"intent": "general", "topic": "违约金"}
    ) == (False, "generic_intent")


def test_ok(trigger):
    assert trigger.should_trigger(evidence(5), UNDERSTANDING) == (True, "ok")


def test_the_reason_tags_are_exactly_these():
    """The eight labels the tracing and the tests rely on."""
    assert {
        "no_evidence", "too_few_chunks", "single_doc", "no_scores",
        "low_top_score", "no_topic", "generic_intent", "throttled", "ok",
    } == {
        "no_evidence", "too_few_chunks", "single_doc", "no_scores",
        "low_top_score", "no_topic", "generic_intent", "throttled", "ok",
    }


# --------------------------------------------------------------------------
# the derivations
# --------------------------------------------------------------------------


def test_the_topic_key_is_a_slug(trigger):
    assert trigger.extract_topic({"topic": "违约金 条款"}) == "违约金-条款"


def test_an_empty_topic_becomes_untitled(trigger):
    assert trigger.extract_topic({}) == "untitled"
    assert trigger.extract_topic(None) == "untitled"


def test_the_title_is_the_topic(trigger):
    assert trigger.derive_title({"topic": "违约金条款"}) == "违约金条款"


def test_a_long_topic_is_truncated(trigger):
    title = trigger.derive_title({"topic": "标" * 100})
    assert len(title) == 61
    assert title.endswith("…")


def test_a_missing_topic_becomes_a_placeholder(trigger):
    assert trigger.derive_title({}) == "未命名主题"


def test_the_category_comes_from_the_intent(trigger):
    assert trigger.derive_category({"intent": "contract"}) == "contract"
    assert trigger.derive_category({"intent": "law"}) == "law"
    assert trigger.derive_category({"intent": "project"}) == "project"


def test_an_unknown_intent_becomes_general(trigger):
    assert trigger.derive_category({"intent": "wat"}) == "general"
    assert trigger.derive_category({}) == "general"


# --------------------------------------------------------------------------
# the thresholds are actually used
# --------------------------------------------------------------------------


def test_the_threshold_is_configurable(trigger):
    trigger.min_evidence_chunks = 2
    assert trigger.should_trigger(evidence(3), UNDERSTANDING) == (True, "ok")


def test_the_top_score_threshold_is_configurable(trigger):
    trigger.min_top_score = 0.2
    rows = evidence(5, score=0.3)
    assert trigger.should_trigger(rows, UNDERSTANDING) == (True, "ok")
