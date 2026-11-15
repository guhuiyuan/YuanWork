"""A document too long to read at once is read in parts.

The core layer holds the long documents - the contracts, the rules, the
policies. Some of them are longer than the model can take in one go.
Those are read in parts, and every conclusion says which part it came
from, so the reader knows whether a document says one thing about a
subject or several.

* AC-E-013 - a document longer than one read is read in parts, and the
  page says which part each conclusion came from
"""

from __future__ import annotations

import json
import re
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
    """A model that answers about the part of the document it was given."""

    def __init__(self):
        self.prompts: list[str] = []

    async def complete(self, task):
        prompt = task.messages[0].content
        self.prompts.append(prompt)

        numbers = re.findall(r"【第 (\d+) 段】", prompt)
        first = int(numbers[0]) if numbers else 0

        answer = {
            "entity_pages": [
                {
                    "title": "验收期限",
                    "summary": "验收期限怎么算。",
                    "key_points": ["验收期限怎么约定"],
                    "conclusions": [
                        {"text": f"验收期限写在第 {first} 段里", "segment": first}
                    ],
                }
            ],
            "concept_pages": [],
            "comparison_tables": [],
        }
        return type("R", (), {"content": json.dumps(answer, ensure_ascii=False)})()


def document(segments: int = 6, size: int = 100) -> dict[str, Any]:
    """A document with the given number of parts."""
    pieces = []
    for index in range(segments):
        pieces.append(
            {
                "text": f"第 {index} 段。" + "内容" * (size // 2),
                "anchor": {"kind": "page", "page": index + 1},
            }
        )
    return {
        "doc_id": "d1",
        "file_name": "长合同.md",
        "file_path": "/kb/合同/长合同.md",
        "source_hash": "h1",
        "text": "\n\n".join(piece["text"] for piece in pieces),
        "segments": pieces,
    }


# --------------------------------------------------------------------------
# AC-E-013 — read in parts
# --------------------------------------------------------------------------


async def test_a_long_document_is_read_in_parts(store):
    """One read cannot hold it - so it is read several times."""
    llm = FakeLLM()
    compiler = CoreCompiler(store, llm, read_chars=250)

    await compiler.compile(document())

    assert len(llm.prompts) > 1, "the document was read in one go"


async def test_a_short_document_is_read_in_one_go(store):
    llm = FakeLLM()
    compiler = CoreCompiler(store, llm, read_chars=100_000)

    await compiler.compile(document(segments=2, size=50))

    assert len(llm.prompts) == 1


async def test_the_segments_are_numbered_over_the_whole_document(store):
    """The second read knows it is the second read."""
    llm = FakeLLM()
    compiler = CoreCompiler(store, llm, read_chars=250)

    await compiler.compile(document())

    # The second read does not start again at zero.
    second = llm.prompts[1]
    assert "【第 2 段】" in second
    assert "【第 0 段】" not in second


async def test_the_page_says_which_part_each_conclusion_came_from(store):
    """The reader can tell a document that says one thing from one that
    says several."""
    llm = FakeLLM()
    compiler = CoreCompiler(store, llm, read_chars=250)

    await compiler.compile(document())

    pages = await store.list_wiki_pages()
    assert len(pages) == 1
    markdown = pages[0]["markdown"]
    assert "（第 0 段）" in markdown
    assert "（第 2 段）" in markdown
    assert "（第 4 段）" in markdown


async def test_the_conclusions_know_their_own_part(store):
    """The part is written down with the conclusion."""
    llm = FakeLLM()
    compiler = CoreCompiler(store, llm, read_chars=250)

    await compiler.compile(document())

    conclusions = await store.list_page_conclusions("core/验收期限")
    assert [c["segment_index"] for c in conclusions] == [0, 2, 4]
    # And each one still points at the place it was read from.
    assert conclusions[0]["anchor"] == {"kind": "page", "page": 1}
    assert conclusions[1]["anchor"] == {"kind": "page", "page": 3}
    assert conclusions[2]["anchor"] == {"kind": "page", "page": 5}


async def test_the_same_page_from_two_parts_is_one_page(store):
    """The parties of a contract are mentioned again at the end."""
    llm = FakeLLM()
    compiler = CoreCompiler(store, llm, read_chars=250)

    result = await compiler.compile(document())

    assert len(result["pages"]) == 1
    pages = await store.list_wiki_pages()
    assert len(pages) == 1


async def test_a_short_document_does_not_say_which_part_it_came_from(store):
    """One part - there is nothing to point at."""
    llm = FakeLLM()
    compiler = CoreCompiler(store, llm, read_chars=100_000)

    await compiler.compile(document(segments=1, size=50))

    pages = await store.list_wiki_pages()
    assert "（第 0 段）" not in pages[0]["markdown"]


async def test_the_read_budget_comes_from_the_settings():
    """How much one read holds is configuration."""
    from yuanwork.config import YuanWorkSettings

    settings = YuanWorkSettings(data_dir=Path("."), core_read_chars=1234)
    assert settings.core_read_chars == 1234
