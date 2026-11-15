"""A revised document does not quietly replace the old one.

When a contract is revised, the page that says what the contract says
has to show both versions: the one that is in effect, and the one that
was. The reader decides which one applies - the system does not get to
decide that for them.

* AC-F-024 - a contradiction is marked, and both versions are kept
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.wiki.core_compiler import CoreCompiler


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeLLM:
    """A model that compiles a document, and judges the contradictions."""

    def __init__(
        self,
        answer: dict[str, Any] | None = None,
        contradictions: dict[str, Any] | None = None,
        error_on_conflict: bool = False,
    ):
        self.answer = answer if answer is not None else {}
        self.contradictions = contradictions if contradictions is not None else {
            "contradictions": []
        }
        self.error_on_conflict = error_on_conflict
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        if self.error_on_conflict and "旧结论" in task.messages[0].content:
            raise RuntimeError("provider 挂了")
        if "旧结论" in task.messages[0].content:
            return type(
                "R", (), {"content": json.dumps(self.contradictions, ensure_ascii=False)}
            )()
        return type(
            "R", (), {"content": json.dumps(self.answer, ensure_ascii=False)}
        )()


def answer(text: str) -> dict[str, Any]:
    """A model answer with one conclusion."""
    return {
        "entity_pages": [
            {
                "title": "验收期限",
                "summary": "验收期限怎么算。",
                "key_points": ["验收期限怎么约定"],
                "conclusions": [{"text": text, "segment": 0}],
            }
        ],
        "concept_pages": [],
        "comparison_tables": [],
    }


def document(**overrides) -> dict[str, Any]:
    doc = {
        "doc_id": "d1",
        "file_name": "采购合同.pdf",
        "file_path": "/kb/合同/采购合同.pdf",
        "source_hash": "h1",
        "text": "验收期限为 30 日。",
        "segments": [
            {"text": "验收期限为 30 日。", "anchor": {"kind": "page", "page": 12}},
        ],
    }
    doc.update(overrides)
    return doc


async def seed_old_version(store, text: str = "验收期限为 30 日") -> None:
    """A page that was compiled before."""
    await store.upsert_page_conclusion(
        conclusion_id="core/验收期限#old",
        page_id="core/验收期限",
        text=text,
        anchor={"kind": "page", "page": 12},
        source_doc_id="d0",
        source_hash="h0",
    )


# --------------------------------------------------------------------------
# AC-F-024 — the contradiction
# --------------------------------------------------------------------------


async def test_a_contradiction_is_marked_and_both_versions_are_kept(store):
    """The old version is not thrown away."""
    await seed_old_version(store)

    llm = FakeLLM(
        answer("验收期限为 15 日"),
        contradictions={
            "contradictions": [
                {
                    "old_index": 0,
                    "new_index": 0,
                    "topic": "验收期限",
                    "reason": "旧版 30 日，新版 15 日",
                }
            ]
        },
    )
    compiler = CoreCompiler(store, llm)

    result = await compiler.compile(document())

    assert result["compiled"] is True
    assert len(result["conflicts"]) == 1

    # Both versions are there.
    conclusions = await store.list_page_conclusions(
        "core/验收期限", active_only=False
    )
    assert len(conclusions) == 2
    texts = {c["text"] for c in conclusions}
    assert texts == {"验收期限为 30 日", "验收期限为 15 日"}

    # They are in the same group - each is the other's counterpart.
    groups = {c["conflict_group"] for c in conclusions}
    assert len(groups) == 1
    assert groups != {""}


async def test_the_version_in_effect_is_the_new_one(store):
    """The new version is the one a question may lean on."""
    await seed_old_version(store)

    llm = FakeLLM(
        answer("验收期限为 15 日"),
        contradictions={
            "contradictions": [
                {"old_index": 0, "new_index": 0, "topic": "验收期限", "reason": "不一样"}
            ]
        },
    )
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    in_effect = await store.list_page_conclusions("core/验收期限")
    assert [c["text"] for c in in_effect] == ["验收期限为 15 日"]


async def test_a_contradiction_produces_a_comparison_table(store):
    """The two versions are put side by side."""
    await seed_old_version(store)
    await store.upsert_file(
        doc_id="d0",
        file_name="采购合同（旧版）.pdf",
        file_path="C:/kb/合同/采购合同（旧版）.pdf",
        file_hash="h0",
        file_type="pdf",
        size_bytes=100,
    )
    await store.upsert_file(
        doc_id="d1",
        file_name="采购合同.pdf",
        file_path="C:/kb/合同/采购合同.pdf",
        file_hash="h1",
        file_type="pdf",
        size_bytes=100,
    )

    llm = FakeLLM(
        answer("验收期限为 15 日"),
        contradictions={
            "contradictions": [
                {
                    "old_index": 0,
                    "new_index": 0,
                    "topic": "验收期限",
                    "reason": "旧版 30 日，新版 15 日",
                }
            ]
        },
    )
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    pages = await store.list_wiki_pages()
    tables = [p for p in pages if p["page_type"] == "comparison"]
    assert len(tables) == 1

    table = tables[0]
    assert "验收期限为 30 日" in table["markdown"]
    assert "验收期限为 15 日" in table["markdown"]
    # Both versions say where they were read from.
    assert "采购合同（旧版）.pdf" in table["markdown"]
    assert "采购合同.pdf" in table["markdown"]
    assert table["tier"] == "core"


async def test_the_old_version_is_not_silently_dropped(store):
    """No contradiction, no deletion of anything the reader may need."""
    await seed_old_version(store)

    llm = FakeLLM(
        answer("验收期限为 30 日"),
        contradictions={"contradictions": []},
    )
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    conclusions = await store.list_page_conclusions(
        "core/验收期限", active_only=False
    )
    # The old conclusion is the same text as the new one - the id is the
    # same, so it is updated rather than doubled.
    assert len(conclusions) == 1


async def test_a_superseded_conclusion_is_dropped_when_it_is_not_contradicted(store):
    """A conclusion the new compilation does not have is not evidence."""
    await seed_old_version(store, text="违约金为 5%")

    llm = FakeLLM(
        answer("验收期限为 15 日"),
        contradictions={"contradictions": []},
    )
    compiler = CoreCompiler(store, llm)

    await compiler.compile(document())

    conclusions = await store.list_page_conclusions(
        "core/验收期限", active_only=False
    )
    assert [c["text"] for c in conclusions] == ["验收期限为 15 日"]


async def test_a_check_that_failed_keeps_the_old_version(store):
    """A check that did not run is not a check that found nothing."""
    await seed_old_version(store)

    llm = FakeLLM(
        answer("验收期限为 15 日"),
        error_on_conflict=True,
    )
    compiler = CoreCompiler(store, llm)

    result = await compiler.compile(document())

    assert result["compiled"] is True
    conclusions = await store.list_page_conclusions(
        "core/验收期限", active_only=False
    )
    texts = {c["text"] for c in conclusions}
    assert texts == {"验收期限为 30 日", "验收期限为 15 日"}


async def test_a_contradiction_on_a_first_compile_is_not_a_contradiction(store):
    """There is nothing to contradict the first time round."""
    llm = FakeLLM(
        answer("验收期限为 15 日"),
        contradictions={
            "contradictions": [
                {"old_index": 0, "new_index": 0, "topic": "验收期限", "reason": "不一样"}
            ]
        },
    )
    compiler = CoreCompiler(store, llm)

    result = await compiler.compile(document())

    assert result["conflicts"] == []
    conclusions = await store.list_page_conclusions("core/验收期限")
    assert len(conclusions) == 1
    assert conclusions[0]["conflict_group"] == ""
