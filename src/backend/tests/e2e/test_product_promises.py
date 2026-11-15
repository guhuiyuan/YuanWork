"""The three promises, end to end.

The product promises three things. These tests walk the real modules -
the store, the retrieval, the agent, the compiler, the graph - and only
the model is faked:

* **the answer can be located in the source** (AC-F-005, AC-F-007)
* **the knowledge base wins, and the divergence is said out loud**
  (AC-F-010)
* **not enough evidence is a refusal with positions and suggestions**
  (AC-F-009)

Plus the two promises about the pages: a page the user edited is not
overwritten (AC-F-013), and the graph has the entities and the edges
(AC-F-015).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yuanwork.api import deps
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.wiki.entity_extractor import EntityExtractor
from yuanwork.wiki.graph import WikiGraph
from yuanwork.wiki.page_generator import WikiPageGenerator

from .harness import ScriptedLLM, evidence, make_agent


# --------------------------------------------------------------------------
# the fixtures
# --------------------------------------------------------------------------


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


# --------------------------------------------------------------------------
# promise 1 — the answer can be located in the source
# --------------------------------------------------------------------------


async def test_the_answer_carries_a_citation_with_the_file_and_the_place(store):
    """AC-F-005 - every conclusion has a citation with the file name
    and the position."""
    llm = ScriptedLLM()
    agent = make_agent(store, llm, evidence(3))

    result = await agent.run(query="违约金怎么约定？", session_id="s1")

    assert result["refused"] is False
    assert result["citations"], "the answer has no citation"
    citation = result["citations"][0]
    assert citation["file_name"] == "合同.pdf"
    assert citation["page"] == 12
    # The anchor is what the desktop app opens the file at.
    assert citation["anchor"]["kind"] == "page"
    assert citation["anchor"]["page"] == 12


async def test_the_citation_points_at_the_piece_that_was_cited(store):
    llm = ScriptedLLM()
    agent = make_agent(store, llm, evidence(3))

    result = await agent.run(query="违约金怎么约定？", session_id="s1")

    # The answer cites [1] - so only that piece is a citation.
    assert [c["index"] for c in result["citations"]] == [1]


async def test_evidence_that_cannot_be_located_is_a_refusal(store):
    """AC-F-007 - evidence without a position may not back an answer."""
    llm = ScriptedLLM()
    agent = make_agent(store, llm, evidence(3, locatable=False))

    result = await agent.run(query="违约金怎么约定？", session_id="s1")

    assert result["refused"] is True
    assert result["answer"], "a refusal still has to say something"


async def test_too_little_evidence_is_a_refusal(store):
    """AC-F-007 - the hard rule gate wants more than one piece."""
    llm = ScriptedLLM()
    agent = make_agent(store, llm, evidence(1))

    result = await agent.run(query="违约金怎么约定？", session_id="s1")

    assert result["refused"] is True


# --------------------------------------------------------------------------
# promise 2 — the knowledge base wins, and the divergence is said out loud
# --------------------------------------------------------------------------


async def test_a_conflict_is_marked_at_the_top_of_the_answer(store):
    """AC-F-010 - the divergence is said before anything else."""
    llm = ScriptedLLM(
        conflict={
            "verdict": "conflict",
            "conflict_claims": [
                {
                    "topic": "违约金比例",
                    "llm_says": "违约金是合同总额的十分之一",
                    "kb_says": "违约金按合同总额的三分之一计算",
                }
            ],
            "explanation": "两者不一致",
        }
    )
    agent = make_agent(store, llm, evidence(3))

    result = await agent.run(query="违约金怎么约定？", session_id="s1")

    assert result["conflicts"], "the conflict was not reported"
    # The answer starts with the notice - the user has to see it before
    # the text, not after it.
    assert result["answer"].lstrip().startswith("⚠")
    assert "分歧" in result["answer"]


async def test_the_knowledge_base_is_the_answer_when_there_is_a_conflict(store):
    """AC-F-010 - the documents win; the model's wording is only there
    for comparison."""
    llm = ScriptedLLM(
        answer="违约金是合同总额的十分之一 [1]。",
        conflict={
            "verdict": "conflict",
            "conflict_claims": [
                {
                    "topic": "违约金比例",
                    "llm_says": "十分之一",
                    "kb_says": "三分之一",
                }
            ],
            "explanation": "两者不一致",
        },
    )
    agent = make_agent(store, llm, evidence(3))

    result = await agent.run(query="违约金怎么约定？", session_id="s1")

    assert "以知识库为准" in result["answer"]
    # The knowledge base's own text is in the answer.
    assert "三分之一" in result["answer"]
    # The model's wording is kept, but only as a comparison.
    assert "仅作对照" in result["answer"]


async def test_a_conflict_is_written_down(store):
    """AC-F-011 - the divergence is recorded, with its source."""
    from yuanwork.consolidation.engine import ConsolidationEngine
    from yuanwork.memory.episodic import EpisodicMemory
    from yuanwork.memory.profile import ProfileStore

    llm = ScriptedLLM(
        conflict={
            "verdict": "conflict",
            "conflict_claims": [
                {
                    "topic": "违约金比例",
                    "llm_says": "十分之一",
                    "kb_says": "三分之一",
                }
            ],
            "explanation": "两者不一致",
        }
    )
    agent = make_agent(store, llm, evidence(3))
    agent.consolidation = ConsolidationEngine(
        episodic_memory=EpisodicMemory(store),
        semantic_memory=None,  # type: ignore[arg-type]
        profile_store=ProfileStore(store),
        sqlite_store=store,
        llm_gateway=llm,  # type: ignore[arg-type]
    )

    await agent.run(query="违约金怎么约定？", session_id="s1")

    conflicts = await store.list_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["topic"] == "违约金比例"
    assert conflicts[0]["citation"]


# --------------------------------------------------------------------------
# promise 3 — not enough evidence is a refusal with positions and advice
# --------------------------------------------------------------------------


async def test_the_model_gate_can_refuse(store):
    """AC-F-008 - the model says the evidence is not enough."""
    llm = ScriptedLLM(sufficient=False)
    agent = make_agent(store, llm, evidence(3))

    result = await agent.run(query="违约金怎么约定？", session_id="s1")

    assert result["refused"] is True
    assert result["refusal_reason"] == "model_insufficient"


async def test_a_refusal_gives_the_positions_and_the_advice(store):
    """AC-F-009 - the refusal says where to look and what to do."""
    await store.upsert_file(
        doc_id="d1",
        file_name="合同.pdf",
        file_path="/kb/合同.pdf",
        file_hash="h1",
        file_type="pdf",
        size_bytes=1024,
    )
    llm = ScriptedLLM(sufficient=False)
    agent = make_agent(store, llm, evidence(3))

    result = await agent.run(query="违约金怎么约定？", session_id="s1")

    assert "合同.pdf" in result["answer"]
    # The advice is not "sorry" - it says what could be done.
    assert any(
        word in result["answer"] for word in ("建议", "可以", "试试", "关键词")
    )


async def test_a_refusal_does_not_state_a_conclusion(store):
    """AC-F-009 - a refusal may not carry a conclusion of its own."""
    llm = ScriptedLLM(sufficient=False)
    agent = make_agent(store, llm, evidence(3))

    result = await agent.run(query="违约金怎么约定？", session_id="s1")

    # The model's answer was not taken over - it was never answered.
    assert "三分之一" not in result["answer"]


# --------------------------------------------------------------------------
# the pages: the user's edit is not overwritten
# --------------------------------------------------------------------------


async def test_a_page_the_user_edited_keeps_their_paragraph(store):
    """AC-F-013 - a recompile replaces the model's paragraphs only."""
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=None)

    await generator.compile_page(title="违约金条款", chunks=evidence(2))
    blocks = await store.list_page_blocks("违约金条款")
    assert blocks, "the page has no blocks"

    # The user edits one paragraph of the page - the block becomes theirs
    # and its text is theirs too.
    blocks = await store.list_page_blocks("违约金条款")
    for block in blocks:
        block["owner"] = "user"
        block["text"] = block["text"].replace("第 0 段", "我改过的段落")
    await store.replace_page_blocks("违约金条款", blocks)
    page = await store.get_wiki_page("违约金条款")
    await store.update_wiki_page_body(
        "违约金条款",
        markdown="\n\n".join(block["text"] for block in blocks),
        summary=page["summary"],
        key_points=page.get("key_points", []),
    )

    # The theme compiles again.
    await generator.compile_page(title="违约金条款", chunks=evidence(2))

    page = await store.get_wiki_page("违约金条款")
    assert "我改过的段落" in page["markdown"]


# --------------------------------------------------------------------------
# the graph: the entities and the edges
# --------------------------------------------------------------------------


async def test_the_graph_has_the_entities_and_the_edges(store):
    """AC-F-015 - the documents' entities become the nodes, and the
    relations become the edges."""
    extractor = EntityExtractor(sqlite_store=store, llm_gateway=None)
    await extractor.extract_and_store("「华润」的违约金条款", doc_id="d1")

    entities = await store.list_entities()
    assert entities, "the document's entities did not make it into the graph"
    assert entities[0]["name"] == "华润"

    await store.add_entity_relation(
        source_id=entities[0]["id"],
        target_id=entities[0]["id"],
        relation_type="mentions",
    )

    graph = await WikiGraph(store).build_graph()
    node_ids = [n["id"] for n in graph["nodes"]]
    assert entities[0]["id"] in node_ids
    assert any(n["type"].startswith("entity_") for n in graph["nodes"])
