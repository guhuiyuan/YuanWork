"""Compiling a page when the user asks for it.

The automatic trigger compiles a page when the evidence cluster is big
enough. The manual one compiles because the user asked - and it writes
the same page, so a manual compile updates the page instead of piling
up a second one.

* AC-F-012 - a page is compiled and its identifier comes back
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.routes import wiki as wiki_routes
from yuanwork.db.sqlite import SQLiteStore


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeSemantic:
    """A semantic memory with some pieces of evidence in it."""

    def __init__(self, chunks: list[dict[str, Any]] | None = None):
        self.chunks = chunks if chunks is not None else []

    async def search_hybrid(self, query, top_k=10, filter_expr=None, **kwargs):
        return list(self.chunks)

    async def search(self, query, top_k=10, filter_expr=None):
        return list(self.chunks)

    async def list_recent(self, limit=20):
        return []


class FakeLLM:
    def __init__(self, content: str = ""):
        self.content = content or (
            "# 违约金条款\n\n## 摘要\n\n这是摘要。\n\n"
            "## 要点\n- 一条要点\n\n## 出处\n[1] 《合同.pdf》第 12 页\n"
        )
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        return type("R", (), {"content": self.content})()


def evidence(count: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "id": f"c{i}",
            "doc_id": "d1",
            "text": f"这是第 {i} 段正文内容。",
            "file_name": "合同.pdf",
            "file_path": "/kb/合同.pdf",
            "meta": {"page": 12 + i},
            "_score": 0.9,
        }
        for i in range(count)
    ]


@pytest.fixture
def client(store) -> TestClient:
    deps.set_sqlite_store(store)
    deps.set_semantic_memory(FakeSemantic(evidence()))  # type: ignore[arg-type]
    deps.set_llm_gateway(FakeLLM())  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(wiki_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


# --------------------------------------------------------------------------
# the compile
# --------------------------------------------------------------------------


def test_a_manual_compile_writes_a_page(client, store):
    resp = client.post("/api/v1/wiki/compile/违约金条款")

    assert resp.status_code == 200
    body = resp.json()
    assert body["compiled"] is True
    assert body["title"] == "违约金条款"
    assert body["page_id"] == "auto/违约金条款"


async def test_the_compiled_page_is_in_the_store(client, store):
    body = client.post("/api/v1/wiki/compile/违约金条款").json()

    page = await store.get_wiki_page(body["page_id"])
    assert page is not None
    assert page["title"] == "违约金条款"


def test_the_manual_compile_writes_the_page_the_trigger_would(client, store):
    """Otherwise a manual compile piles up a second page."""
    body = client.post("/api/v1/wiki/compile/违约金条款").json()

    assert body["page_id"].startswith("auto/")


def test_the_compile_reads_the_evidence_of_the_topic(client):
    body = client.post("/api/v1/wiki/compile/违约金条款").json()

    assert body["chunk_count"] == 3


def test_a_topic_without_evidence_compiles_nothing(client, store):
    deps.set_semantic_memory(FakeSemantic([]))  # type: ignore[arg-type]

    resp = client.post("/api/v1/wiki/compile/没有资料的主题")

    assert resp.status_code == 200
    body = resp.json()
    assert body["compiled"] is False
    assert body["reason"] == "no_evidence"


def test_a_search_that_fails_says_so(client):
    class BrokenSemantic:
        async def search_hybrid(self, query, top_k=10, filter_expr=None, **kwargs):
            raise RuntimeError("索引坏了")

    deps.set_semantic_memory(BrokenSemantic())  # type: ignore[arg-type]

    resp = client.post("/api/v1/wiki/compile/违约金条款")

    assert resp.status_code == 200
    body = resp.json()
    assert body["compiled"] is False
    assert body["reason"] == "search_failed"


async def test_a_compile_that_fails_says_so(client, store):
    class BrokenStore(SQLiteStore):
        async def upsert_wiki_page(self, *args, **kwargs):
            raise RuntimeError("写不进去")

    broken = BrokenStore(store.db_path)
    try:
        await broken.initialize()
        deps.set_sqlite_store(broken)

        resp = client.post("/api/v1/wiki/compile/违约金条款")

        assert resp.status_code == 200
        body = resp.json()
        assert body["compiled"] is False
        assert body["reason"] == "compile_failed"
    finally:
        await broken.close()


def test_the_error_response_does_not_echo_the_exception(client):
    class BrokenSemantic:
        async def search_hybrid(self, query, top_k=10, filter_expr=None, **kwargs):
            raise RuntimeError("C:\\Users\\someone\\秘密路径")

    deps.set_semantic_memory(BrokenSemantic())  # type: ignore[arg-type]

    body = client.post("/api/v1/wiki/compile/违约金条款").json()

    assert "秘密路径" not in json_dumps(body)


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
