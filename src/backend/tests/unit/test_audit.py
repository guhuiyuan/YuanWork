"""Tests for the LLM-call audit log (Stage 3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


# ===== AuditLogger core =====


@pytest.fixture
def audit_log_path(temp_dir: Path) -> Path:
    return temp_dir / "audit.jsonl"


def test_audit_logger_writes_jsonl(audit_log_path: Path):
    """Each log_llm_call appends one JSON line."""
    from yuanwork.privacy.audit import AuditLogger

    al = AuditLogger(audit_log_path)
    al.log_llm_call(
        model="gpt-4o",
        task_type="answer",
        prompt_summary="What is 2+2?",
        response_summary="4",
        prompt_tokens=10,
        completion_tokens=1,
        duration_ms=420.0,
        success=True,
    )
    assert audit_log_path.exists()
    lines = audit_log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["type"] == "llm_call"
    assert entry["model"] == "gpt-4o"
    assert entry["task_type"] == "answer"
    assert entry["success"] is True
    assert "timestamp" in entry


def test_audit_logger_truncates_long_summaries(audit_log_path: Path):
    """Prompt/response summaries are truncated to 200 chars."""
    from yuanwork.privacy.audit import AuditLogger

    al = AuditLogger(audit_log_path)
    long = "x" * 1000
    al.log_llm_call(
        model="m", task_type="general",
        prompt_summary=long, response_summary=long,
    )
    entry = al.read_all()[0]
    assert len(entry["prompt_summary"]) == 200
    assert len(entry["response_summary"]) == 200


def test_audit_logger_records_failure(audit_log_path: Path):
    """Failed calls are recorded with success=False and an error message."""
    from yuanwork.privacy.audit import AuditLogger

    al = AuditLogger(audit_log_path)
    al.log_llm_call(
        model="gpt-4o", task_type="answer",
        prompt_summary="x", response_summary="",
        success=False, error="rate limit exceeded",
    )
    entry = al.read_all()[0]
    assert entry["success"] is False
    assert entry["error"] == "rate limit exceeded"


def test_audit_logger_clear(audit_log_path: Path):
    """clear() deletes the file."""
    from yuanwork.privacy.audit import AuditLogger

    al = AuditLogger(audit_log_path)
    al.log_llm_call(model="m", task_type="general", prompt_summary="x", response_summary="y")
    assert audit_log_path.exists()
    al.clear()
    assert not audit_log_path.exists()
    assert al.read_all() == []


def test_audit_logger_data_access(audit_log_path: Path):
    """log_data_access also appends an entry."""
    from yuanwork.privacy.audit import AuditLogger

    al = AuditLogger(audit_log_path)
    al.log_data_access("read", "kb/foo.pdf", {"chunks": 5})
    entries = al.read_all()
    assert len(entries) == 1
    assert entries[0]["type"] == "data_access"
    assert entries[0]["action"] == "read"
    assert entries[0]["resource"] == "kb/foo.pdf"
    assert entries[0]["details"] == {"chunks": 5}


def test_audit_logger_creates_parent_dirs(tmp_path: Path):
    """Parent directory is created automatically."""
    from yuanwork.privacy.audit import AuditLogger

    nested = tmp_path / "deep" / "nest" / "audit.jsonl"
    al = AuditLogger(nested)
    al.log_llm_call(model="m", task_type="t", prompt_summary="x", response_summary="y")
    assert nested.exists()


# ===== Query hash (privacy preserving) =====


def test_query_hash_is_stable_for_same_prompt():
    """Same messages produce the same hash."""
    from yuanwork.llm.gateway import LLMMessage, _query_hash

    msgs = [
        LLMMessage(role="user", content="Hello"),
        LLMMessage(role="assistant", content="Hi there"),
    ]
    h1 = _query_hash(msgs)
    h2 = _query_hash(msgs)
    assert h1 == h2
    assert len(h1) == 16  # 16-char hex prefix


def test_query_hash_differs_for_different_prompts():
    from yuanwork.llm.gateway import LLMMessage, _query_hash

    h1 = _query_hash([LLMMessage(role="user", content="Hello")])
    h2 = _query_hash([LLMMessage(role="user", content="World")])
    assert h1 != h2


def test_query_hash_handles_unicode():
    """CJK prompts hash deterministically."""
    from yuanwork.llm.gateway import LLMMessage, _query_hash

    msgs = [LLMMessage(role="user", content="金额 1280元 是什么意思?")]
    h = _query_hash(msgs)
    assert len(h) == 16
    # Stable
    assert h == _query_hash(msgs)


# ===== LLMGateway audit integration =====


def _fake_litellm_response(content: str = "OK", prompt_tokens: int = 10, completion_tokens: int = 5):
    """Build a fake litellm completion response object."""

    class _Usage:
        def __init__(self, p, c):
            self.prompt_tokens = p
            self.completion_tokens = c
            self.total_tokens = p + c

    class _Choice:
        def __init__(self, content):
            self.message = type("M", (), {"content": content, "role": "assistant"})()
            self.finish_reason = "stop"

    class _Resp:
        def __init__(self, content, p, c):
            self.choices = [_Choice(content)]
            self.usage = _Usage(p, c)
    return _Resp(content, prompt_tokens, completion_tokens)


@pytest.fixture
def gw_with_audit(temp_dir: Path, monkeypatch):
    """LLMGateway with an AuditLogger pointed at temp_dir, and litellm mocked."""
    from yuanwork.llm.gateway import LLMGateway
    from yuanwork.privacy.audit import AuditLogger
    from yuanwork.config import YuanWorkSettings

    # Minimal models config
    (temp_dir / "models.yaml").write_text(
        """
models:
  fake-mini:
    provider: openai
    model_name: gpt-4o-mini
routing:
  general: fake-mini
""",
        encoding="utf-8",
    )
    settings = YuanWorkSettings(config_dir=temp_dir)
    audit = AuditLogger(temp_dir / "audit.jsonl")
    gw = LLMGateway(settings, audit_logger=audit)
    return gw, audit, temp_dir


@pytest.mark.asyncio
async def test_llm_call_is_audited(gw_with_audit, monkeypatch):
    """A successful complete() call writes one audit entry."""
    gw, audit, _ = gw_with_audit

    from yuanwork.llm.gateway import LLMMessage, LLMTask

    monkeypatch.setattr(
        "yuanwork.llm.gateway.litellm.completion",
        lambda **kw: _fake_litellm_response("Hello there", 12, 4),
    )

    task = LLMTask(
        task_type="general",
        messages=[LLMMessage(role="user", content="hi")],
        temperature=0.0,
    )
    result = await gw.complete(task)
    assert result.content == "Hello there"

    entries = audit.read_all()
    assert len(entries) == 1
    e = entries[0]
    assert e["type"] == "llm_call"
    assert e["model"] == "fake-mini"
    assert e["task_type"] == "general"
    assert e["success"] is True
    assert e["prompt_tokens"] == 12
    assert e["completion_tokens"] == 4
    assert e["duration_ms"] >= 0
    # Prompt summary should start with the 16-char query hash
    assert e["prompt_summary"].startswith("#") and len(e["prompt_summary"]) >= 17


@pytest.mark.asyncio
async def test_llm_call_audits_failure(gw_with_audit, monkeypatch):
    """A failing complete() call still writes an audit entry with success=False."""
    gw, audit, _ = gw_with_audit

    from yuanwork.llm.gateway import LLMMessage, LLMTask

    def _explode(**kw):
        raise RuntimeError("upstream 503")

    monkeypatch.setattr(
        "yuanwork.llm.gateway.litellm.completion",
        _explode,
    )

    task = LLMTask(
        task_type="answer",
        messages=[LLMMessage(role="user", content="question")],
    )
    with pytest.raises(RuntimeError, match="upstream 503"):
        await gw.complete(task)

    entries = audit.read_all()
    assert len(entries) == 1
    assert entries[0]["success"] is False
    assert "upstream 503" in entries[0]["error"]


@pytest.mark.asyncio
async def test_no_audit_when_logger_is_none(temp_dir: Path, monkeypatch):
    """When audit_logger is None, no log file is created."""
    from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask
    from yuanwork.config import YuanWorkSettings

    (temp_dir / "models.yaml").write_text(
        """
models:
  fake-mini:
    provider: openai
    model_name: gpt-4o-mini
routing:
  general: fake-mini
""",
        encoding="utf-8",
    )
    settings = YuanWorkSettings(config_dir=temp_dir)
    gw = LLMGateway(settings, audit_logger=None)  # explicitly None

    monkeypatch.setattr(
        "yuanwork.llm.gateway.litellm.completion",
        lambda **kw: _fake_litellm_response("ok"),
    )

    task = LLMTask(
        task_type="general",
        messages=[LLMMessage(role="user", content="hi")],
    )
    await gw.complete(task)
    # No audit file should be created
    assert not (temp_dir / "audit.jsonl").exists()
    # No exception means the None path works


# ===== API routes =====


def _make_minimal_app_with_audit(audit_logger):
    """Build a tiny FastAPI app with the audit router and our logger mounted."""
    from fastapi import FastAPI
    from yuanwork.api.routes.audit import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.audit_logger = audit_logger
    return app


def test_audit_route_list_returns_entries(temp_dir: Path):
    """GET /api/v1/audit returns entries newest-first."""
    from fastapi.testclient import TestClient
    from yuanwork.privacy.audit import AuditLogger

    al = AuditLogger(temp_dir / "audit.jsonl")
    for i in range(3):
        al.log_llm_call(
            model="gpt-4o", task_type="general",
            prompt_summary=f"call {i}", response_summary="ok",
            prompt_tokens=10, completion_tokens=2,
        )
    app = _make_minimal_app_with_audit(al)
    client = TestClient(app)
    resp = client.get("/api/v1/audit")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 3
    assert data["count"] == 3
    # Newest first: call 2 first
    assert "call 2" in data["entries"][0]["prompt_summary"]


def test_audit_route_list_respects_limit(temp_dir: Path):
    """GET /api/v1/audit?limit=2 returns at most 2 entries."""
    from fastapi.testclient import TestClient
    from yuanwork.privacy.audit import AuditLogger

    al = AuditLogger(temp_dir / "audit.jsonl")
    for i in range(5):
        al.log_llm_call(model="m", task_type="t",
                        prompt_summary=str(i), response_summary="r")
    app = _make_minimal_app_with_audit(al)
    client = TestClient(app)
    resp = client.get("/api/v1/audit?limit=2")
    data = resp.json()
    assert data["total"] == 5  # total counts all
    assert data["count"] == 2  # count is what we returned


def test_audit_route_stats_aggregates(temp_dir: Path):
    """GET /api/v1/audit/stats returns per-model / per-task-type counts."""
    from fastapi.testclient import TestClient
    from yuanwork.privacy.audit import AuditLogger

    al = AuditLogger(temp_dir / "audit.jsonl")
    # Mix of models and tasks
    al.log_llm_call(model="gpt-4o", task_type="answer", prompt_summary="x", response_summary="y",
                    prompt_tokens=100, completion_tokens=20, success=True)
    al.log_llm_call(model="gpt-4o", task_type="understand", prompt_summary="x", response_summary="y",
                    prompt_tokens=50, completion_tokens=10, success=True)
    al.log_llm_call(model="claude", task_type="answer", prompt_summary="x", response_summary="y",
                    prompt_tokens=80, completion_tokens=15, success=False,
                    error="rate limit")
    al.log_data_access("read", "kb/foo.pdf")  # not llm_call — should not count

    app = _make_minimal_app_with_audit(al)
    client = TestClient(app)
    resp = client.get("/api/v1/audit/stats")
    data = resp.json()
    assert data["llm_calls"] == 3
    assert data["failures"] == 1
    assert data["total_prompt_tokens"] == 230
    assert data["total_completion_tokens"] == 45
    assert data["by_model"]["gpt-4o"] == 2
    assert data["by_model"]["claude"] == 1
    assert data["by_task_type"]["answer"] == 2


def test_audit_route_clear(temp_dir: Path):
    """DELETE /api/v1/audit removes all entries."""
    from fastapi.testclient import TestClient
    from yuanwork.privacy.audit import AuditLogger

    al = AuditLogger(temp_dir / "audit.jsonl")
    al.log_llm_call(model="m", task_type="t", prompt_summary="x", response_summary="y")
    al.log_llm_call(model="m", task_type="t", prompt_summary="x", response_summary="y")
    app = _make_minimal_app_with_audit(al)
    client = TestClient(app)
    resp = client.delete("/api/v1/audit")
    assert resp.status_code == 200
    assert resp.json()["cleared"] == 2
    # Subsequent list is empty
    resp = client.get("/api/v1/audit")
    assert resp.json()["count"] == 0


def test_audit_route_503_when_not_initialized():
    """If app.state.audit_logger is None, the routes return 503."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from yuanwork.api.routes.audit import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    # Intentionally do NOT set app.state.audit_logger
    client = TestClient(app)
    resp = client.get("/api/v1/audit")
    assert resp.status_code == 503
    assert "not initialized" in resp.json()["detail"].lower()
