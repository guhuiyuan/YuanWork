"""The audit file's I/O and the audit routes.

* how entries are appended, read and cleared
* the routes: list, stats, clear
* the 503 when no logger exists
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api.routes.audit import router
from yuanwork.privacy.audit import AuditLogger


@pytest.fixture
def logger(tmp_path: Path) -> AuditLogger:
    return AuditLogger(tmp_path / "logs" / "audit.jsonl")


def log(logger: AuditLogger, model: str = "m", task: str = "answer"):
    logger.log_llm_call(
        model=model, task_type=task, prompt_hash="abc1234567890123",
        prompt_tokens=10, completion_tokens=2, duration_ms=1.0, success=True,
    )


@pytest.fixture
def client(logger: AuditLogger) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.audit_logger = logger
    return TestClient(app)


# --------------------------------------------------------------------------
# the file
# --------------------------------------------------------------------------


def test_the_parent_directories_are_created(tmp_path: Path):
    path = tmp_path / "deep" / "logs" / "audit.jsonl"
    AuditLogger(path)
    assert path.parent.exists()


def test_each_call_appends_one_line(logger):
    log(logger)
    log(logger)
    lines = logger.audit_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
    assert json.loads(lines[0])["model"] == "m"


def test_the_entries_are_read_back_in_order(logger):
    log(logger, model="first")
    log(logger, model="second")
    entries = logger.read_all()
    assert [e["model"] for e in entries] == ["first", "second"]


def test_a_broken_line_is_skipped(logger):
    log(logger)
    with logger.audit_path.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
    log(logger)
    entries = logger.read_all()
    assert len(entries) == 2


def test_clearing_removes_the_file(logger):
    log(logger)
    assert logger.audit_path.exists()
    logger.clear()
    assert not logger.audit_path.exists()


def test_clearing_an_absent_file_is_silent(logger):
    logger.clear()  # must not raise
    assert logger.read_all() == []


# --------------------------------------------------------------------------
# the routes
# --------------------------------------------------------------------------


def test_the_entries_are_listed_newest_first(client):
    log(client.app.state.audit_logger, model="first")
    log(client.app.state.audit_logger, model="second")
    body = client.get("/api/v1/audit").json()
    assert body["total"] == 2
    assert body["entries"][0]["model"] == "second"


def test_the_limit_caps_the_result(client):
    for i in range(5):
        log(client.app.state.audit_logger, model=str(i))
    body = client.get("/api/v1/audit?limit=2").json()
    assert len(body["entries"]) == 2
    assert body["total"] == 5


def test_the_statistics_are_aggregated(client):
    logger = client.app.state.audit_logger
    log(logger, model="a", task="answer")
    log(logger, model="a", task="answer")
    log(logger, model="b", task="understand")
    body = client.get("/api/v1/audit/stats").json()
    assert body["llm_calls"] == 3
    assert body["by_model"] == {"a": 2, "b": 1}
    assert body["by_task_type"] == {"answer": 2, "understand": 1}
    assert body["total_prompt_tokens"] == 30
    assert body["failures"] == 0


def test_the_failures_are_counted(client):
    logger = client.app.state.audit_logger
    logger.log_llm_call(
        model="a", task_type="answer", prompt_hash="abc1234567890123",
        prompt_tokens=1, completion_tokens=1, duration_ms=1.0, success=False,
        error_kind="unknown",
    )
    body = client.get("/api/v1/audit/stats").json()
    assert body["failures"] == 1


def test_the_log_can_be_cleared(client):
    log(client.app.state.audit_logger)
    body = client.delete("/api/v1/audit").json()
    assert body["cleared"] == 1
    assert client.get("/api/v1/audit").json()["total"] == 0


def test_without_a_logger_the_routes_answer_503():
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    client = TestClient(app)
    for method, path in (
        ("get", "/api/v1/audit"),
        ("get", "/api/v1/audit/stats"),
        ("delete", "/api/v1/audit"),
    ):
        resp = getattr(client, method)(path)
        assert resp.status_code == 503, path
        assert "not initialized" in resp.json()["detail"]
