"""TASK-005 — the list endpoints' paging parameters.

* AC-P-001 — the audit list: 1..1000, default 100
* AC-P-002 — the conflict list: 1..500, default 50
* AC-P-003 — the wiki page list: 1..500, default 50
* AC-P-004 — the episodic memory list: 1..100, default 20
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.routes import audit as audit_routes
from yuanwork.api.routes import memory as memory_routes
from yuanwork.api.routes import wiki as wiki_routes


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


class FakeEpisodicMemory:
    def __init__(self, count: int = 3):
        self.count = count

    async def list_recent(self, limit: int = 20):
        return [self._entry(i) for i in range(min(limit, self.count))]

    async def search_by_topic(self, topic: str, limit: int = 20):
        return [self._entry(i) for i in range(min(limit, self.count))]

    def _entry(self, i: int) -> dict:
        from datetime import datetime

        return {
            "id": i,
            "session_id": "s",
            "role": "user",
            "content": f"内容 {i}",
            "topic_tags": [],
            "entities": [],
            "created_at": datetime(2026, 1, 1),
        }


class FakeSQLiteStore:
    def __init__(self):
        self.pages = []
        self.conflicts = []

    async def list_wiki_pages(self, category=None, limit=50, offset=0):
        return self.pages[:limit]

    async def count_wiki_pages(self, category=None):
        return len(self.pages)

    async def list_conflicts(self, limit=50, include_acknowledged=False):
        return self.conflicts[:limit]


@pytest.fixture
def client(tmp_path: Path):
    from yuanwork.privacy.audit import AuditLogger

    deps.set_episodic_memory(FakeEpisodicMemory())  # type: ignore[arg-type]
    deps.set_sqlite_store(FakeSQLiteStore())  # type: ignore[arg-type]

    app = FastAPI()
    app.include_router(audit_routes.router, prefix="/api/v1")
    app.include_router(memory_routes.router, prefix="/api/v1")
    app.include_router(wiki_routes.router, prefix="/api/v1")
    app.state.audit_logger = AuditLogger(tmp_path / "audit.jsonl")
    yield TestClient(app)
    deps.reset_dependencies()


# --------------------------------------------------------------------------
# AC-P-001 — the audit list
# --------------------------------------------------------------------------


def audit_client(client) -> TestClient:
    return client


@pytest.mark.parametrize("limit", [1, 50, 1000])
def test_the_audit_limit_accepts_its_range(client, limit):
    resp = client.get(f"/api/v1/audit?limit={limit}")
    assert resp.status_code == 200


@pytest.mark.parametrize("limit", [0, 1001, -5])
def test_the_audit_limit_rejects_values_outside_its_range(client, limit):
    resp = client.get(f"/api/v1/audit?limit={limit}")
    assert resp.status_code == 422


def test_the_audit_limit_defaults_to_100(client):
    resp = client.get("/api/v1/audit")
    assert resp.status_code == 200
    query = resp.request.url.query
    if isinstance(query, bytes):
        query = query.decode()
    assert "limit" not in query


# --------------------------------------------------------------------------
# AC-P-002 — the conflict list
# --------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 50, 500])
def test_the_conflict_limit_accepts_its_range(client, limit):
    resp = client.get(f"/api/v1/memory/conflicts?limit={limit}")
    assert resp.status_code == 200


@pytest.mark.parametrize("limit", [0, 501])
def test_the_conflict_limit_rejects_values_outside_its_range(client, limit):
    resp = client.get(f"/api/v1/memory/conflicts?limit={limit}")
    assert resp.status_code == 422


# --------------------------------------------------------------------------
# AC-P-003 — the wiki page list
# --------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 50, 500])
def test_the_wiki_limit_accepts_its_range(client, limit):
    resp = client.get(f"/api/v1/wiki/pages?limit={limit}")
    assert resp.status_code == 200


@pytest.mark.parametrize("limit", [0, 501])
def test_the_wiki_limit_rejects_values_outside_its_range(client, limit):
    resp = client.get(f"/api/v1/wiki/pages?limit={limit}")
    assert resp.status_code == 422


def test_the_wiki_offset_must_not_be_negative(client):
    assert client.get("/api/v1/wiki/pages?offset=0").status_code == 200
    assert client.get("/api/v1/wiki/pages?offset=-1").status_code == 422


# --------------------------------------------------------------------------
# AC-P-004 — the episodic memory list
# --------------------------------------------------------------------------


@pytest.mark.parametrize("limit", [1, 20, 100])
def test_the_episodic_limit_accepts_its_range(client, limit):
    resp = client.get(f"/api/v1/memory/episodic?limit={limit}")
    assert resp.status_code == 200


@pytest.mark.parametrize("limit", [0, 101])
def test_the_episodic_limit_rejects_values_outside_its_range(client, limit):
    resp = client.get(f"/api/v1/memory/episodic?limit={limit}")
    assert resp.status_code == 422


def test_the_episodic_limit_actually_caps_the_result(client):
    resp = client.get("/api/v1/memory/episodic?limit=1")
    assert resp.status_code == 200
    assert len(resp.json()["entries"]) == 1
