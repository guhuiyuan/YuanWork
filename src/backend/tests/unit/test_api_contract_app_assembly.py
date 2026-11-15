"""TASK-007 — how the application is put together.

* AC-F-011 — the health endpoint reports four components
* AC-F-012 — the CORS allow-list
* AC-F-014 — the eight components the lifespan mounts on the app state
* AC-E-001 — the conflict field-name mismatch (the agent produces
  `llm_says`/`kb_says`, the response model wants `llm_answer`/`kb_evidence`)
* AC-E-010 — no endpoint is authenticated
* AC-E-011 — the "include acknowledged" flag has no effect
* AC-E-012 — the consolidate endpoint builds an engine without a gateway
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from yuanwork.api.app import create_app
from yuanwork.api.schemas import ConflictInfo
from yuanwork.config import YuanWorkSettings


EXPECTED_CORS_ORIGINS = {
    "http://localhost:5173",
    "http://localhost:1420",
    "tauri://localhost",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:1420",
}

LIFESPAN_COMPONENTS = [
    "sqlite_store",
    "llm_gateway",
    "audit_logger",
    "semantic_memory",
    "episodic_memory",
    "profile_store",
    "consolidation_engine",
    "wiki_compile_trigger",
]


@pytest.fixture(scope="module")
def live_app(tmp_path_factory):
    """An app that has actually been through its lifespan."""
    data_dir = tmp_path_factory.mktemp("yuanwork_data")
    app = create_app(YuanWorkSettings(data_dir=data_dir))
    with TestClient(app) as client:
        yield app, client


# --------------------------------------------------------------------------
# AC-F-012 — the CORS allow-list
# --------------------------------------------------------------------------


def test_the_cors_allow_list(live_app):
    app, _client = live_app
    cors = [m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware"]
    assert cors, "the CORS middleware is not installed"
    origins = set(cors[0].kwargs["allow_origins"])
    assert origins == EXPECTED_CORS_ORIGINS


def test_cors_allows_any_method_and_header(live_app):
    app, _client = live_app
    cors = [m for m in app.user_middleware if m.cls.__name__ == "CORSMiddleware"][0]
    assert cors.kwargs["allow_methods"] == ["*"]
    assert cors.kwargs["allow_headers"] == ["*"]
    assert cors.kwargs["allow_credentials"] is True


def test_a_preflight_from_an_allowed_origin_is_accepted(live_app):
    _app, client = live_app
    resp = client.options(
        "/api/v1/health",
        headers={
            "Origin": "http://localhost:5173",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:5173"


def test_a_preflight_from_an_unknown_origin_gets_no_allow_origin(live_app):
    _app, client = live_app
    resp = client.options(
        "/api/v1/health",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in resp.headers


# --------------------------------------------------------------------------
# AC-F-014 — the components the lifespan mounts
# --------------------------------------------------------------------------


def test_the_lifespan_mounts_the_eight_components(live_app):
    app, _client = live_app
    for name in LIFESPAN_COMPONENTS:
        assert hasattr(app.state, name), name


def test_the_mounted_components_are_not_none(live_app):
    app, _client = live_app
    for name in LIFESPAN_COMPONENTS:
        assert getattr(app.state, name) is not None, name


# --------------------------------------------------------------------------
# AC-F-011 — the health endpoint
# --------------------------------------------------------------------------


def test_the_health_endpoint_reports_four_components(live_app):
    _app, client = live_app
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["components"]) == {
        "sqlite", "llm", "semantic_memory", "episodic_memory",
    }
    assert body["status"] == "ok"
    assert body["network_mode"]
    assert body["data_dir"]


def test_the_health_endpoint_reports_the_embedding_state(live_app):
    _app, client = live_app
    body = client.get("/api/v1/health").json()
    semantic = body["components"]["semantic_memory"]
    assert semantic["status"] in ("ok", "loading", "deferred")
    assert semantic["embedding_model"]


def test_the_version_endpoint(live_app):
    _app, client = live_app
    resp = client.get("/api/v1/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["version"]
    assert body["python_version"].startswith("3")
    assert body["platform"]


# --------------------------------------------------------------------------
# AC-E-001 — the conflict field-name mismatch
# --------------------------------------------------------------------------


def test_the_conflict_model_wants_llm_answer_and_kb_evidence():
    conflict = ConflictInfo(type="llm_vs_kb")
    assert hasattr(conflict, "llm_answer")
    assert hasattr(conflict, "kb_evidence")


def test_the_agent_field_names_are_ignored_by_the_model():
    """The orchestrator produces `llm_says` / `kb_says`; the response
    model only knows `llm_answer` / `kb_evidence`, so the two sides of
    the conflict arrive empty."""
    conflict = ConflictInfo(
        type="llm_vs_kb",
        llm_says="模型说 A",
        kb_says="知识库说 B",
    )
    assert conflict.llm_answer == ""
    assert conflict.kb_evidence == ""


def test_the_orchestrator_really_uses_the_other_names():
    from yuanwork.orchestrator import agent

    source = inspect.getsource(agent)
    assert "llm_says" in source
    assert "kb_says" in source


# --------------------------------------------------------------------------
# AC-E-010 — no authentication
# --------------------------------------------------------------------------


def test_no_endpoint_requires_credentials(live_app):
    _app, client = live_app
    for path in ("/api/v1/health", "/api/v1/version", "/api/v1/kb/files",
                 "/api/v1/wiki/pages", "/api/v1/audit"):
        resp = client.get(path)
        assert resp.status_code != 401, path
        assert resp.status_code != 403, path
        assert "www-authenticate" not in resp.headers, path


# --------------------------------------------------------------------------
# AC-E-011 — the "include acknowledged" flag
# --------------------------------------------------------------------------


def test_the_conflict_flag_has_no_effect(live_app):
    _app, client = live_app
    without = client.get("/api/v1/memory/conflicts")
    with_flag = client.get("/api/v1/memory/conflicts?include_acknowledged=true")
    assert without.status_code == 200
    assert with_flag.status_code == 200
    assert without.json()["conflicts"] == with_flag.json()["conflicts"]


def test_no_endpoint_acknowledges_a_conflict(live_app):
    _app, client = live_app
    schema = client.app.openapi()
    paths = set(schema["paths"])
    assert not any("acknowledge" in p for p in paths)


# --------------------------------------------------------------------------
# the consolidate endpoint's engine
# --------------------------------------------------------------------------


def test_the_consolidate_endpoint_uses_the_live_engine():
    """The engine the app already has.

    The consolidation engine needs the model gateway (design §1.2 - its
    dependencies are the relation store and the model gateway), and a
    second engine built inside the endpoint would have neither the
    gateway nor the trigger state.
    """
    from yuanwork.api.routes import memory as memory_routes

    source = inspect.getsource(memory_routes)
    assert "ConsolidationEngine(" not in source
    assert "optional_consolidation_engine()" in source
