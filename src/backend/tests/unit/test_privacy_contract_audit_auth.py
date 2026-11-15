"""The audit interface has no authentication, and errors echo their text.

Both facts matter for the privacy posture: any local process can read
the audit (which contains the first 200 characters of every prompt and
answer), and an unhandled exception puts its text into the response.
"""

from __future__ import annotations

from pathlib import Path

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api.app import create_app
from yuanwork.config import YuanWorkSettings
from yuanwork.privacy.audit import AuditLogger


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(YuanWorkSettings(data_dir=tmp_path / "data"))
    app.state.audit_logger = AuditLogger(tmp_path / "logs" / "audit.jsonl")
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------
# no authentication
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/v1/audit"),
        ("get", "/api/v1/audit/stats"),
        ("delete", "/api/v1/audit"),
    ],
)
def test_the_audit_routes_need_no_credentials(client, method, path):
    resp = getattr(client, method)(path)
    assert resp.status_code != 401
    assert resp.status_code != 403
    assert "www-authenticate" not in resp.headers


def test_no_authorization_header_is_needed(client):
    resp = client.get("/api/v1/audit")
    assert resp.status_code == 200


def test_the_other_routes_need_no_credentials_either(client):
    for path in ("/api/v1/health", "/api/v1/version", "/api/v1/kb/files",
                 "/api/v1/wiki/pages"):
        resp = client.get(path)
        assert resp.status_code != 401, path
        assert resp.status_code != 403, path


def test_the_audit_content_is_readable_without_credentials(client):
    """The audit holds the first 200 characters of every prompt — so it
    is readable by anything that can reach the port."""
    logger = client.app.state.audit_logger
    logger.log_llm_call(
        model="m", task_type="answer",
        prompt_hash="abc1234567890123",
        prompt_tokens=1, completion_tokens=1, duration_ms=1.0, success=True,
    )
    body = client.get("/api/v1/audit").json()
    # the entry holds the hash, not the question or the answer
    assert body["entries"][0]["prompt_hash"] == "abc1234567890123"
    assert "prompt_summary" not in body["entries"][0]
    assert "response_summary" not in body["entries"][0]


# --------------------------------------------------------------------------
# the exception text
# --------------------------------------------------------------------------


def test_an_unhandled_exception_does_not_echo_its_text(tmp_path: Path):
    """A local path must not reach the client."""
    app = create_app(YuanWorkSettings(data_dir=tmp_path / "data"))

    @app.get("/boom")
    def boom():
        raise RuntimeError("内部路径 D:\\secret\\store.db")

    client = TestClient(app, raise_server_exceptions=False)
    body = client.get("/boom").json()
    assert body["code"] == "INTERNAL_ERROR"
    assert "secret" not in json.dumps(body, ensure_ascii=False)


def test_the_exception_text_stays_out_of_the_response(tmp_path: Path):
    """The key name of an environment variable is not the user's problem
    to solve - and it is not the client's to read."""
    app = create_app(YuanWorkSettings(data_dir=tmp_path / "data"))

    @app.get("/boom")
    def boom():
        raise RuntimeError("MINIMAX_API_KEY 读取失败")

    client = TestClient(app, raise_server_exceptions=False)
    body = client.get("/boom").json()
    assert "MINIMAX_API_KEY" not in json.dumps(body, ensure_ascii=False)
    assert set(body) == {"error", "code"}
