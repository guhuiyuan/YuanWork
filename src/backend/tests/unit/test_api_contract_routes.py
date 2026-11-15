"""TASK-001 — the endpoint inventory and its grouping.

The frontend (and any script) talks to the backend over exactly these
38 endpoints. Adding or moving one is a breaking change for the desktop
client, so the list is pinned here.

* AC-F-001 — the endpoint inventory and the `/api/v1` prefix
* AC-F-002 — the seven tag groups
"""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.api.app import create_app
from yuanwork.config import YuanWorkSettings


EXPECTED_ENDPOINTS = {
    # health
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/version"),
    # ingest
    ("POST", "/api/v1/ingest/dir"),
    ("POST", "/api/v1/ingest/file"),
    ("POST", "/api/v1/ingest/rebuild"),
    ("GET", "/api/v1/ingest/status/{job_id}"),
    # kb
    ("GET", "/api/v1/kb/files"),
    ("DELETE", "/api/v1/kb/files/{doc_id}"),
    ("POST", "/api/v1/kb/files/{doc_id}/tier"),
    ("GET", "/api/v1/kb/tiers"),
    ("GET", "/api/v1/kb/stats"),
    ("GET", "/api/v1/kb/terms/{term}/context"),
    # chat
    ("POST", "/api/v1/chat/stream"),
    ("POST", "/api/v1/chat"),
    ("POST", "/api/v1/chat/cancel/{session_id}"),
    # memory
    ("GET", "/api/v1/memory/episodic"),
    ("GET", "/api/v1/memory/profile"),
    ("DELETE", "/api/v1/memory/profile/{key}"),
    ("GET", "/api/v1/memory/conflicts"),
    ("GET", "/api/v1/memory/rules/pending"),
    ("POST", "/api/v1/memory/rules/{entity_id}/approve"),
    ("POST", "/api/v1/memory/consolidate"),
    # wiki
    ("GET", "/api/v1/wiki/pages"),
    ("GET", "/api/v1/wiki/pages/{page_id}/backlinks"),
    ("GET", "/api/v1/wiki/pages/{page_id}/conclusions"),
    ("GET", "/api/v1/wiki/pages/{page_id}/blocks"),
    ("GET", "/api/v1/wiki/pages/{page_id}"),
    ("PUT", "/api/v1/wiki/pages/{page_id}"),
    ("GET", "/api/v1/wiki/graph"),
    ("POST", "/api/v1/wiki/compile/{topic}"),
    # audit
    ("GET", "/api/v1/audit"),
    ("GET", "/api/v1/audit/stats"),
    ("DELETE", "/api/v1/audit"),
    # workspace
    ("POST", "/api/v1/workspace/folders"),
    ("POST", "/api/v1/workspace/files"),
    ("GET", "/api/v1/workspace/entries"),
    ("DELETE", "/api/v1/workspace/entries/{entry_id}"),
    # interactions
    ("POST", "/api/v1/interactions/{kind}"),
}

EXPECTED_TAGS = {
    "health", "ingest", "kb", "chat", "memory", "wiki", "audit", "workspace",
    "interactions",
}


@pytest.fixture
def app(tmp_path: Path):
    settings = YuanWorkSettings(data_dir=tmp_path / "data")
    return create_app(settings)


def endpoints(app) -> set[tuple[str, str]]:
    """The (method, path) pairs the app exposes."""
    schema = app.openapi()
    found = set()
    for path, operations in schema["paths"].items():
        for method in operations:
            if method == "parameters":
                continue
            found.add((method.upper(), path))
    return found


def test_the_endpoint_inventory_is_exactly_as_listed(app):
    found = endpoints(app)
    assert found == EXPECTED_ENDPOINTS
    assert len(found) == 38


def test_every_endpoint_uses_the_version_prefix(app):
    for _method, path in endpoints(app):
        assert path.startswith("/api/v1/"), path


def test_the_eight_tag_groups(app):
    schema = app.openapi()
    tags = {
        tag
        for operations in schema["paths"].values()
        for operation in operations.values()
        if isinstance(operation, dict)
        for tag in operation.get("tags", [])
    }
    assert tags == EXPECTED_TAGS


def test_each_endpoint_belongs_to_exactly_one_group(app):
    schema = app.openapi()
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if method == "parameters":
                continue
            own = {t for t in operation.get("tags", []) if t in EXPECTED_TAGS}
            assert len(own) == 1, f"{method} {path} -> {operation.get('tags')}"


def test_the_endpoints_are_grouped_by_business_area(app):
    schema = app.openapi()
    by_tag: dict[str, set[tuple[str, str]]] = {}
    for path, operations in schema["paths"].items():
        for method, operation in operations.items():
            if method == "parameters":
                continue
            for tag in set(operation.get("tags", [])):
                if tag in EXPECTED_TAGS:
                    by_tag.setdefault(tag, set()).add((method.upper(), path))

    assert len(by_tag["health"]) == 2
    assert len(by_tag["ingest"]) == 4
    assert len(by_tag["kb"]) == 6
    assert len(by_tag["chat"]) == 3
    assert len(by_tag["memory"]) == 7
    assert len(by_tag["wiki"]) == 8
    assert len(by_tag["audit"]) == 3
    assert len(by_tag["workspace"]) == 4


def test_the_wiki_page_route_accepts_a_slash_in_the_id(app):
    """The `:path` converter is what lets a page id contain a slash.

    In the OpenAPI document the converter shows up as a plain parameter,
    so the two wiki page routes are distinguished by their suffix.
    """
    paths = {path for _m, path in endpoints(app)}
    assert "/api/v1/wiki/pages/{page_id}" in paths
    assert "/api/v1/wiki/pages/{page_id}/backlinks" in paths
    assert "/api/v1/wiki/pages/{page_id}/conclusions" in paths
    assert "/api/v1/wiki/pages/{page_id}/blocks" in paths


def test_the_openapi_document_is_served(app):
    from fastapi.testclient import TestClient

    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"] == "YuanWork API"
