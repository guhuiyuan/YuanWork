"""The harness for the end-to-end tests.

Everything here is real except the model: the store, the retrieval, the
agent and the compiler are the ones the product runs with. The model is
scripted so that each test can say what the model should answer.
"""

from __future__ import annotations

import json
from typing import Any

from yuanwork.api import deps
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.orchestrator.agent import AgentLoop
from yuanwork.retrieval.confidence import ConfidenceScorer


def reply(content: str):
    return type("R", (), {"content": content})


class ScriptedLLM:
    """A model that answers each step with what the test wants."""

    def __init__(
        self,
        *,
        sufficient: bool = True,
        answer: str = "违约金按合同总额的三分之一计算 [1]。",
        conflict: dict[str, Any] | None = None,
    ):
        self.sufficient = sufficient
        self.answer = answer
        self.conflict = conflict or {
            "verdict": "consistent",
            "conflict_claims": [],
            "explanation": "没有分歧",
        }
        self.calls: list[str] = []

    async def complete(self, task):
        self.calls.append(task.task_type)
        if task.task_type == "evidence_gate":
            return reply(
                json.dumps(
                    {
                        "sufficient": self.sufficient,
                        "reason": "证据覆盖了问题" if self.sufficient else "证据不够",
                    },
                    ensure_ascii=False,
                )
            )
        if task.task_type == "conflict_check":
            return reply(json.dumps(self.conflict, ensure_ascii=False))
        if task.task_type == "answer":
            return reply(self.answer)
        return reply(
            json.dumps(
                {"intent": "general", "topic": "违约金", "file_hint": ""},
                ensure_ascii=False,
            )
        )

    async def stream(self, task):
        for piece in self.answer.split(" "):
            yield piece + " "


class FakeSemantic:
    """The semantic memory, with the pieces of evidence the test set up."""

    def __init__(self, chunks: list[dict[str, Any]] | None = None):
        self.chunks = chunks if chunks is not None else []

    async def search(self, query, top_k=50, filter_expr=None):
        return list(self.chunks)

    async def search_hybrid(self, query, top_k=10, filter_expr=None, **kwargs):
        return list(self.chunks)


class FakeRetriever:
    def __init__(self, evidence: list[dict[str, Any]]):
        self.evidence = evidence

    async def search(
        self, query, top_k=10, doc_ids=None, file_types=None, min_score=0.0
    ):
        return list(self.evidence)


def evidence(count: int = 3, *, locatable: bool = True) -> list[dict[str, Any]]:
    """The pieces of evidence the agent will find.

    A piece without a position cannot be located in its document - and
    evidence like that may not back an answer.
    """
    chunks = []
    for i in range(count):
        chunk = {
            "id": f"c{i}",
            "doc_id": "d1",
            "text": f"违约金按合同总额的三分之一计算（第 {i} 段）。",
            "file_name": "合同.pdf",
            "file_path": "/kb/合同.pdf",
            "_score": 0.9 - i * 0.05,
        }
        if locatable:
            chunk["meta"] = {"page": 12 + i}
        chunks.append(chunk)
    return chunks


class TieredLLM:
    """A model that compiles a core document and answers a question.

    Everything else it is asked - the query understanding, the evidence
    gate, the conflict check - it answers the way the product expects.
    """

    def __init__(
        self,
        conclusions: list[dict[str, Any]] | None = None,
        answer: str = "验收期限为 30 日 [1]。",
        contradictions: dict[str, Any] | None = None,
    ):
        self.conclusions = conclusions if conclusions is not None else [
            {"text": "验收期限为 30 日", "segment": 0},
            {"text": "验收期限自交付之日起计算", "segment": 0},
        ]
        self.answer = answer
        self.contradictions = contradictions or {"contradictions": []}
        self.calls: list[str] = []

    async def complete(self, task):
        self.calls.append(task.task_type)
        prompt = task.messages[0].content if task.messages else ""

        if task.task_type == "wiki_compile":
            if "旧结论" in prompt:
                # The contradiction check.
                return reply(
                    json.dumps(self.contradictions, ensure_ascii=False)
                )
            return reply(
                json.dumps(
                    {
                        "concept_pages": [
                            {
                                "title": "验收期限",
                                "summary": "验收期限怎么约定。",
                                "key_points": ["验收期限怎么约定"],
                                "conclusions": self.conclusions,
                            }
                        ],
                        "entity_pages": [],
                        "comparison_tables": [],
                    },
                    ensure_ascii=False,
                )
            )
        if task.task_type == "answer":
            return reply(self.answer)
        if task.task_type == "evidence_gate":
            return reply(
                json.dumps(
                    {"sufficient": True, "reason": "证据覆盖了问题"},
                    ensure_ascii=False,
                )
            )
        if task.task_type == "conflict_check":
            return reply(
                json.dumps(
                    {
                        "verdict": "consistent",
                        "conflict_claims": [],
                        "explanation": "没有分歧",
                    },
                    ensure_ascii=False,
                )
            )
        return reply(
            json.dumps(
                {"intent": "general", "topic": "验收期限", "file_hint": ""},
                ensure_ascii=False,
            )
        )

    async def stream(self, task):
        yield self.answer


class RecordingSemantic:
    """The vector store, and a record of what was written."""

    def __init__(self):
        self.added: list[dict[str, Any]] = []

    async def add_chunks(self, chunks):
        self.added.extend(chunks)
        return [c["id"] for c in chunks]

    async def delete_by_doc_id(self, doc_id):
        return None

    async def search(self, query, top_k=50, filter_expr=None):
        return []

    async def search_hybrid(self, query, top_k=10, filter_expr=None, **kwargs):
        return []


class RecordingLongTail:
    """The long tail - and a record of whether it was asked at all."""

    def __init__(self, chunks: list[dict[str, Any]] | None = None):
        self.chunks = chunks if chunks is not None else []
        self.calls: list[dict[str, Any]] = []

    async def search(self, query, top_k=10, doc_ids=None, **kwargs):
        self.calls.append({"query": query, "doc_ids": doc_ids})
        return list(self.chunks)


def make_tiered_agent(
    store: SQLiteStore, llm: Any, longtail: Any
) -> AgentLoop:
    """An agent that looks at the core layer first."""
    from yuanwork.retrieval.tiered import TieredRetriever

    deps.set_sqlite_store(store)
    agent = AgentLoop(
        llm_gateway=llm,  # type: ignore[arg-type]
        semantic_memory=RecordingSemantic(),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        sqlite_store=store,
    )
    agent.tiered = TieredRetriever(sqlite_store=store, retriever=longtail)
    return agent


def make_agent(
    store: SQLiteStore, llm: Any, chunks: list[dict[str, Any]]
) -> AgentLoop:
    """An agent with the real steps and a faked model."""
    deps.set_sqlite_store(store)
    agent = AgentLoop(
        llm_gateway=llm,  # type: ignore[arg-type]
        semantic_memory=FakeSemantic(chunks),  # type: ignore[arg-type]
        episodic_memory=None,  # type: ignore[arg-type]
        consolidation_engine=None,
        wiki_compile_trigger=None,
    )
    agent.retriever = FakeRetriever(chunks)  # type: ignore[assignment]
    agent.scorer = ConfidenceScorer()  # type: ignore[assignment]
    return agent
