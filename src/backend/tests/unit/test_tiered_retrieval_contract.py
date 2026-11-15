"""The core layer answers first, and the long tail only if it has to.

The core layer holds the documents other documents are read against;
their conclusions were compiled into pages when they came in. The long
tail holds the scattered everyday material, and it is searched only
when the core layer cannot answer.

* AC-F-025 - the core layer is searched first, and the long tail only
  when the core layer is not enough
* AC-P-009 - when the core layer has enough, it answers within two
  seconds and the long tail is not searched at all
* AC-E-016 - when the two layers disagree, the core layer wins and the
  answer says so
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.retrieval.tiered import (
    TIER_CORE,
    TIER_LONGTAIL,
    TieredRetriever,
    conflict_note,
    contradicts,
    terms,
)


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeRetriever:
    """The long tail."""

    def __init__(self, chunks: list[dict[str, Any]] | None = None):
        self.chunks = chunks if chunks is not None else []
        self.calls: list[dict[str, Any]] = []

    async def search(self, query, top_k=10, doc_ids=None, **kwargs):
        self.calls.append({"query": query, "top_k": top_k, "doc_ids": doc_ids})
        return list(self.chunks)


async def seed_core(store, text: str, page_id: str = "core/验收期限") -> None:
    """A compiled conclusion, with the place it was read from."""
    await store.upsert_page_conclusion(
        conclusion_id=f"{page_id}#1",
        page_id=page_id,
        text=text,
        anchor={"kind": "page", "page": 12},
        source_doc_id="d1",
    )


async def seed_file(store, doc_id: str = "d1", name: str = "采购合同.pdf") -> None:
    await store.upsert_file(
        doc_id=doc_id,
        file_name=name,
        file_path=f"C:/kb/{name}",
        file_hash="h1",
        file_type="pdf",
        size_bytes=100,
    )


# --------------------------------------------------------------------------
# AC-F-025 — the core layer first
# --------------------------------------------------------------------------


async def test_the_core_layer_is_searched_first(store):
    """A compiled conclusion is the evidence - it was compiled already."""
    await seed_file(store)
    await seed_core(store, "验收期限为 30 日")

    tiered = TieredRetriever(store, FakeRetriever())

    result = await tiered.search("采购合同的验收期限是多久")

    assert result["core_found"] is True
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["text"] == "验收期限为 30 日"
    assert result["evidence"][0]["tier"] == TIER_CORE


async def test_the_core_evidence_carries_its_place(store):
    """A conclusion the reader cannot check cannot back an answer."""
    await seed_file(store)
    await seed_core(store, "验收期限为 30 日")

    tiered = TieredRetriever(store, FakeRetriever())

    result = await tiered.search("验收期限是多久")

    item = result["evidence"][0]
    assert item["anchor"] == {"kind": "page", "page": 12}
    assert item["file_name"] == "采购合同.pdf"


async def test_the_long_tail_is_searched_when_the_core_cannot_answer(store):
    """The core layer is silent - the long tail has to answer."""
    await seed_file(store)
    await seed_core(store, "验收期限为 30 日")

    tail = FakeRetriever([{"id": "c1", "text": "验收期限在附件里", "_score": 0.5}])
    tiered = TieredRetriever(store, tail)

    result = await tiered.search("今天午饭吃什么")

    assert result["longtail_searched"] is True
    assert tail.calls, "the long tail was never searched"
    assert result["evidence"][0]["tier"] == TIER_LONGTAIL


# --------------------------------------------------------------------------
# AC-P-009 — the short circuit
# --------------------------------------------------------------------------


async def test_the_long_tail_is_not_searched_when_the_core_is_enough(store):
    """The extra latency buys noise - and noise dilutes the conclusion."""
    await seed_file(store)
    await seed_core(store, "验收期限为 30 日")
    await seed_core(store, "违约金为合同金额的 5%", page_id="core/违约金")

    tail = FakeRetriever([{"id": "c1", "text": "随便一段", "_score": 0.5}])
    tiered = TieredRetriever(store, tail)

    result = await tiered.search("验收期限和违约金分别是多少")

    assert result["longtail_searched"] is False
    assert tail.calls == [], "the long tail was searched anyway"
    assert len(result["evidence"]) == 2


async def test_the_core_layer_answers_within_two_seconds(store):
    """The core layer has to be quick - it is on the way a question is
    answered."""
    import time

    await seed_file(store)
    await seed_core(store, "验收期限为 30 日")

    tiered = TieredRetriever(store, FakeRetriever())

    started = time.perf_counter()
    await tiered.search("验收期限是多久")
    elapsed = time.perf_counter() - started

    assert elapsed < 2.0, f"the core layer took {elapsed:.2f} s"


async def test_one_core_conclusion_is_not_enough(store):
    """A single conclusion is a hint, not an answer - the long tail is
    still searched."""
    await seed_file(store)
    await seed_core(store, "验收期限为 30 日")

    tail = FakeRetriever([{"id": "c1", "text": "验收期限在附件里", "_score": 0.5}])
    tiered = TieredRetriever(store, tail, min_core=2)

    result = await tiered.search("验收期限是多久")

    assert result["longtail_searched"] is True
    assert tail.calls


# --------------------------------------------------------------------------
# AC-E-016 — the two layers disagree
# --------------------------------------------------------------------------


async def test_the_two_layers_are_compared(store):
    """A slice that says something different is not ignored."""
    await seed_file(store)
    await seed_core(store, "验收期限为 30 日")

    tail = FakeRetriever(
        [{"id": "c1", "text": "验收期限为 15 日", "_score": 0.5}]
    )
    # One core conclusion is not enough on its own - so the long tail is
    # searched too, and the two layers can be compared.
    tiered = TieredRetriever(store, tail)

    result = await tiered.search("验收期限是多久")

    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert conflict["core"] == "验收期限为 30 日"
    assert "15 日" in conflict["longtail"]


async def test_the_core_layer_wins_and_the_answer_says_so(store):
    """The core layer is the one in effect, and the reader is told."""
    await seed_file(store)
    await seed_core(store, "验收期限为 30 日")

    tail = FakeRetriever(
        [{"id": "c1", "text": "验收期限为 15 日", "_score": 0.5}]
    )
    # One core conclusion is not enough on its own - so the long tail is
    # searched too, and the two layers can be compared.
    tiered = TieredRetriever(store, tail)

    result = await tiered.search("验收期限是多久")

    # The core conclusion comes first: it is the one the answer leans on.
    assert result["evidence"][0]["tier"] == TIER_CORE
    assert result["evidence"][0]["text"] == "验收期限为 30 日"
    # The slice is marked, not thrown away quietly.
    assert result["evidence"][1].get("tier_conflict") is True

    note = conflict_note(result["conflicts"])
    assert "核心层" in note
    assert "长尾层" in note


async def test_two_layers_that_agree_are_not_a_conflict(store):
    """The same number in both layers is not a disagreement."""
    await seed_file(store)
    await seed_core(store, "验收期限为 30 日")

    tail = FakeRetriever(
        [{"id": "c1", "text": "验收期限为 30 日，自 2026 年 1 月 1 日起执行", "_score": 0.5}]
    )
    # One core conclusion is not enough on its own - so the long tail is
    # searched too, and the two layers can be compared.
    tiered = TieredRetriever(store, tail)

    result = await tiered.search("验收期限是多久")

    assert result["conflicts"] == []


async def test_the_note_is_empty_when_nothing_disagrees():
    assert conflict_note([]) == ""


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------


def test_two_shared_words_make_a_subject():
    assert terms("验收期限") == {"验收", "收期", "期限"}


def test_the_stopwords_do_not_make_a_subject():
    """「的」 is in every text - it is not a shared subject."""
    assert "的" not in terms("我的合同")
    assert len(terms("我的合同") & terms("他的合同")) == 1


def test_a_slice_about_another_thing_is_not_a_contradiction():
    core = {"text": "验收期限为 30 日"}
    other = {"text": "会议室的投影仪坏了"}
    assert contradicts(core, other) is False


def test_a_slice_with_the_same_number_agrees():
    core = {"text": "验收期限为 30 日"}
    same = {"text": "验收期限为 30 日，自 2026 年 1 月 1 日起执行"}
    assert contradicts(core, same) is False


def test_a_slice_with_another_number_contradicts():
    core = {"text": "验收期限为 30 日"}
    other = {"text": "验收期限为 15 日"}
    assert contradicts(core, other) is True
