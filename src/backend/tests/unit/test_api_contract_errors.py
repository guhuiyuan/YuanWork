"""TASK-006 — the error semantics.

An error response carries a **code** the client can branch on and a
**message** the user can read. It does not carry the exception text:
that can hold a local path, an upstream endpoint, or a piece of the
user's own data. The exception goes to the local log.

The codes are also what keeps "the model is down" apart from "the
documents do not answer this" - mixing the two would tell the user
their question has no answer when the truth is that the machine could
not think.

* AC-E-005 — a broken file is recorded and the rest still gets indexed
* AC-E-009 — a model failure is not shown as "not enough evidence"
* the unified error body
* the exception text stays in the log
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.app import create_app
from yuanwork.api.errors import ERROR_MESSAGES, classify_exception, error_payload
from yuanwork.api.routes.audit import router as audit_router
from yuanwork.api.routes.chat import router as chat_router
from yuanwork.config import YuanWorkSettings


# --------------------------------------------------------------------------
# the unified body
# --------------------------------------------------------------------------


def test_the_error_body_has_a_code_and_a_message():
    payload = error_payload("MODEL_UNAVAILABLE")

    assert set(payload) == {"error", "code"}
    assert payload["code"] == "MODEL_UNAVAILABLE"
    assert payload["error"]


def test_every_code_has_a_readable_message():
    for code in ERROR_MESSAGES:
        assert error_payload(code)["error"]
        assert error_payload(code)["code"] == code


def test_an_unknown_code_still_answers_readably():
    payload = error_payload("SOMETHING_NEW")

    assert payload["code"] == "SOMETHING_NEW"
    assert payload["error"] == ERROR_MESSAGES["INTERNAL_ERROR"]


def test_the_classifier_never_returns_the_exception_text():
    class Boom(Exception):
        pass

    exc = Boom("内部细节：数据库路径 D:\\secret\\store.db")

    code = classify_exception(exc)

    assert code in ERROR_MESSAGES
    assert "store.db" not in code


# --------------------------------------------------------------------------
# the global handler
# --------------------------------------------------------------------------


@pytest.fixture
def boom_app(tmp_path: Path):
    """An app with one route that always raises."""
    app = create_app(YuanWorkSettings(data_dir=tmp_path / "data"))

    @app.get("/boom")
    def boom():
        raise RuntimeError("内部细节：数据库路径 D:\\secret\\store.db")

    return app


def test_an_unhandled_exception_becomes_a_500(boom_app):
    client = TestClient(boom_app, raise_server_exceptions=False)
    resp = client.get("/boom")
    assert resp.status_code == 500


def test_the_global_handler_does_not_echo_the_exception_text(boom_app):
    """The local path in the exception must not reach the client."""
    client = TestClient(boom_app, raise_server_exceptions=False)

    body = client.get("/boom").json()

    assert "内部细节" not in json.dumps(body, ensure_ascii=False)
    assert "store.db" not in json.dumps(body, ensure_ascii=False)
    assert "D:\\\\secret" not in json.dumps(body, ensure_ascii=False)


def test_the_global_handler_answers_with_the_unified_body(boom_app):
    client = TestClient(boom_app, raise_server_exceptions=False)

    body = client.get("/boom").json()

    assert set(body) == {"error", "code"}
    assert body["code"] == "INTERNAL_ERROR"
    assert body["error"] == ERROR_MESSAGES["INTERNAL_ERROR"]


def test_a_route_raising_http_exception_keeps_its_own_detail(boom_app):
    @boom_app.get("/not-found")
    def not_found():
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="没有这个东西")

    client = TestClient(boom_app)
    resp = client.get("/not-found")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "没有这个东西"


def test_a_validation_error_still_answers_422(boom_app):
    """The request was wrong - that is the user's mistake, not ours."""
    client = TestClient(boom_app)
    resp = client.post("/api/v1/chat", json={"query": ""})
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# the blocking chat endpoint
# --------------------------------------------------------------------------


class FailingAgentLoop:
    def __init__(self, **kwargs):
        pass

    async def run(self, query, session_id, history=None, opts=None):
        raise RuntimeError("模型连接失败：api.minimaxi.com")


@pytest.fixture
def chat_client(monkeypatch):
    deps.set_llm_gateway(object())  # type: ignore[arg-type]
    deps.set_semantic_memory(object())  # type: ignore[arg-type]
    deps.set_episodic_memory(object())  # type: ignore[arg-type]
    deps.set_consolidation_engine(object())  # type: ignore[arg-type]
    deps.set_wiki_compile_trigger(object())  # type: ignore[arg-type]

    monkeypatch.setattr(
        "yuanwork.orchestrator.agent.AgentLoop", FailingAgentLoop, raising=True
    )

    app = FastAPI()
    app.include_router(chat_router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


def test_the_blocking_chat_answers_500(chat_client):
    resp = chat_client.post("/api/v1/chat", json={"query": "hello"})
    assert resp.status_code == 500


def test_the_blocking_chat_does_not_echo_the_exception_text(chat_client):
    """The upstream endpoint must not leak into the response."""
    resp = chat_client.post("/api/v1/chat", json={"query": "hello"})

    body = json.dumps(resp.json(), ensure_ascii=False)
    assert "模型连接失败" not in body
    assert "api.minimaxi.com" not in body


def test_the_blocking_chat_answers_with_the_unified_body(chat_client):
    resp = chat_client.post("/api/v1/chat", json={"query": "hello"})

    detail = resp.json()["detail"]
    assert set(detail) == {"error", "code"}
    assert detail["code"] in ERROR_MESSAGES


# --------------------------------------------------------------------------
# AC-E-009 — a model failure is not "not enough evidence"
# --------------------------------------------------------------------------


@pytest.fixture
def agent_store():
    """The file store the agent's localize step reaches for."""

    class FakeSQLite:
        async def list_files(self, limit=100, offset=0, file_type=None, search=None):
            return []

        async def count_files(self, file_type=None, search=None) -> int:
            return 0

    deps.set_sqlite_store(FakeSQLite())  # type: ignore[arg-type]
    yield
    deps.reset_dependencies()


async def test_a_model_failure_is_not_shown_as_insufficient_evidence(agent_store):
    """The two must be told apart, or the user is told the wrong thing."""
    from yuanwork.orchestrator.agent import MODEL_UNAVAILABLE_MESSAGE, AgentLoop

    class FakeLLM:
        async def complete(self, task):
            raise RuntimeError("模型连接失败：api.minimaxi.com")

        async def stream(self, task):
            raise RuntimeError("模型连接失败：api.minimaxi.com")
            yield ""  # pragma: no cover

    agent = AgentLoop(
        llm_gateway=FakeLLM(),
        semantic_memory=object(),
        episodic_memory=object(),
    )

    class NeverRefuse:
        def score(self, evidence):
            return {
                "confidence": 0.9,
                "level": "high",
                "top_score": 0.9,
                "score_gap": 0.1,
                "evidence_count": 2,
            }

    class TwoEvidence:
        async def search(self, query, top_k=10, doc_ids=None, file_types=None, min_score=0.0):
            return [
                {"id": "c1", "doc_id": "d1", "text": "原文", "page": 3, "_score": 0.9},
                {"id": "c2", "doc_id": "d1", "text": "原文", "page": 4, "_score": 0.8},
            ]

    agent.scorer = NeverRefuse()
    agent.retriever = TwoEvidence()

    result = await agent.run(query="问题", session_id="s1")

    assert result["refusal_reason"] == "model_unavailable"
    assert "模型" in result["answer"]
    # it must not read like a refusal because the documents were silent
    assert "没有找到足够明确的证据" not in result["answer"]
    # and the exception text did not travel along
    assert "api.minimaxi.com" not in json.dumps(result, ensure_ascii=False, default=str)


async def test_a_refusal_still_says_the_evidence_was_not_enough(agent_store):
    """The other side of the distinction: a refusal stays a refusal."""
    from yuanwork.orchestrator.agent import AgentLoop

    class FakeLLM:
        async def complete(self, task):
            return type("R", (), {"content": "答案"})()

    agent = AgentLoop(
        llm_gateway=FakeLLM(),
        semantic_memory=object(),
        episodic_memory=object(),
    )

    class AlwaysRefuse:
        def score(self, evidence):
            return {
                "confidence": 0.0,
                "level": "low",
                "top_score": 0.0,
                "score_gap": 0.0,
                "evidence_count": 0,
            }

    class NoEvidence:
        async def search(self, query, top_k=10, doc_ids=None, file_types=None, min_score=0.0):
            return []

    agent.scorer = AlwaysRefuse()
    agent.retriever = NoEvidence()

    result = await agent.run(query="问题", session_id="s1")

    assert result["refused"] is True
    assert result["refusal_reason"] == "insufficient_evidence"
    assert "没有找到足够明确的证据" in result["answer"]


# --------------------------------------------------------------------------
# the audit endpoints without a logger
# --------------------------------------------------------------------------


@pytest.fixture
def audit_client_without_logger():
    app = FastAPI()
    app.include_router(audit_router, prefix="/api/v1")
    # deliberately no app.state.audit_logger
    return TestClient(app)


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/audit"),
        ("get", "/api/v1/audit/stats"),
        ("delete", "/api/v1/audit"),
    ],
)
def test_the_audit_endpoints_answer_503_without_a_logger(
    audit_client_without_logger, method, path
):
    resp = getattr(audit_client_without_logger, method)(path)
    assert resp.status_code == 503
    assert "not initialized" in resp.json()["detail"]
