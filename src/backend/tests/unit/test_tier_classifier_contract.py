"""Which layer a document belongs to.

Two layers: the core layer holds the documents that other documents are
read against; the long tail holds everything else. The user always has
the last word.

* AC-F-021 - every document is placed in a layer, and the user's own
  choice is not overruled
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.ingestion.tier import TierClassifier


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeLLM:
    """A model that answers the layer question."""

    def __init__(self, tier: str = "core", error: Exception | None = None):
        self.tier = tier
        self.error = error
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        if self.error is not None:
            raise self.error
        return type(
            "R",
            (),
            {"content": json.dumps({"tier": self.tier, "reason": "模型判定"})},
        )()


def document(**overrides) -> dict[str, Any]:
    doc = {
        "doc_id": "d1",
        "file_path": "/kb/资料/某文件.pdf",
        "file_name": "某文件.pdf",
        "file_type": "pdf",
        "file_hash": "h1",
        "text_length": 1000,
    }
    doc.update(overrides)
    return doc


# --------------------------------------------------------------------------
# the rules
# --------------------------------------------------------------------------


async def test_a_contract_goes_to_the_core_layer(store):
    classifier = TierClassifier(store)

    result = await classifier.classify(
        document(doc_id="d1", file_path="/kb/合同/采购合同.pdf", file_name="采购合同.pdf")
    )

    assert result["tier"] == "core"
    assert result["decided_by"] == "rule"


async def test_a_policy_document_goes_to_the_core_layer(store):
    classifier = TierClassifier(store)

    result = await classifier.classify(
        document(doc_id="d1", file_path="/kb/差旅制度.pdf", file_name="差旅制度.pdf")
    )

    assert result["tier"] == "core"


async def test_a_big_document_goes_to_the_core_layer(store):
    """A document with conclusions of its own is core, whatever it is
    called."""
    classifier = TierClassifier(store, core_size=20_000)

    result = await classifier.classify(
        document(doc_id="d1", file_name="第三季度总结.pdf", text_length=30_000)
    )

    assert result["tier"] == "core"
    assert result["decided_by"] == "rule"


async def test_a_small_document_goes_to_the_long_tail(store):
    classifier = TierClassifier(store, core_size=20_000)

    result = await classifier.classify(
        document(doc_id="d1", file_name="会议纪要.pdf", text_length=500)
    )

    assert result["tier"] == "longtail"


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------


async def test_the_model_decides_the_documents_the_rules_cannot(store):
    llm = FakeLLM("core")
    classifier = TierClassifier(store, llm, core_size=20_000, model_size=8_000)

    result = await classifier.classify(
        document(doc_id="d1", file_name="某文件.pdf", text_length=12_000)
    )

    assert result["tier"] == "core"
    assert result["decided_by"] == "model"
    assert llm.tasks, "the model was never asked"


async def test_a_small_document_is_not_put_to_the_model(store):
    """A note is a note - and every question costs a call."""
    llm = FakeLLM("core")
    classifier = TierClassifier(store, llm, core_size=20_000, model_size=8_000)

    result = await classifier.classify(
        document(doc_id="d1", file_name="某文件.pdf", text_length=500)
    )

    assert result["tier"] == "longtail"
    assert llm.tasks == [], "the model was asked about a note"


async def test_a_model_that_fails_does_not_stop_the_build(store):
    llm = FakeLLM(error=RuntimeError("provider 挂了"))
    classifier = TierClassifier(store, llm, core_size=20_000, model_size=8_000)

    result = await classifier.classify(
        document(doc_id="d1", file_name="某文件.pdf", text_length=12_000)
    )

    assert result["tier"] == "longtail"
    assert result["decided_by"] == "rule"


# --------------------------------------------------------------------------
# the user's choice
# --------------------------------------------------------------------------


async def test_the_users_choice_is_not_overruled(store):
    await store.set_doc_tier_by_user("d1", "core")
    classifier = TierClassifier(store)

    # The rules would put it in the long tail.
    result = await classifier.classify(
        document(doc_id="d1", file_name="会议纪要.pdf", text_length=100)
    )

    assert result["tier"] == "core"
    assert result["decided_by"] == "user"


async def test_the_users_choice_is_not_overruled_by_the_model(store):
    await store.set_doc_tier_by_user("d1", "longtail")
    llm = FakeLLM("core")
    classifier = TierClassifier(store, llm, core_size=20_000, model_size=8_000)

    result = await classifier.classify(
        document(doc_id="d1", file_name="某文件.pdf", text_length=12_000)
    )

    assert result["tier"] == "longtail"
    assert result["decided_by"] == "user"
    assert llm.tasks == [], "the model was asked about a document the user placed"


async def test_the_users_choice_survives_a_second_judgement(store):
    await store.set_doc_tier_by_user("d1", "core")
    classifier = TierClassifier(store)

    await classifier.classify(document(doc_id="d1", file_name="会议纪要.pdf"))

    record = await store.get_doc_tier("d1")
    assert record["tier"] == "core"
    assert record["decided_by"] == "user"


# --------------------------------------------------------------------------
# the record
# --------------------------------------------------------------------------


async def test_the_judgement_is_written_down(store):
    """A decision nobody can see is a decision nobody can correct."""
    classifier = TierClassifier(store)

    await classifier.classify(document(doc_id="d1", file_name="采购合同.pdf"))

    record = await store.get_doc_tier("d1")
    assert record is not None
    assert record["tier"] == "core"
    assert record["decided_by"] == "rule"
    assert record["decided_at"]
    assert record["source_hash"] == "h1"


async def test_the_documents_of_a_layer_can_be_listed(store):
    classifier = TierClassifier(store)
    await classifier.classify(
        document(doc_id="d1", file_name="采购合同.pdf", file_path="/kb/采购合同.pdf")
    )
    await classifier.classify(
        document(doc_id="d2", file_name="会议纪要.pdf", file_path="/kb/会议纪要.pdf")
    )

    core = await store.list_doc_tiers(tier="core")
    longtail = await store.list_doc_tiers(tier="longtail")

    assert [r["doc_id"] for r in core] == ["d1"]
    assert [r["doc_id"] for r in longtail] == ["d2"]


async def test_a_document_without_an_identifier_does_not_stop_the_build(store):
    classifier = TierClassifier(store)

    result = await classifier.classify({"file_name": "某文件.pdf"})

    assert result["tier"] == "longtail"
