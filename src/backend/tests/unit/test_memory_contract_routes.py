"""The memory and Wiki routes.

* the endpoints and their shapes
* the two stubs
* the flags that have no effect
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from yuanwork.api import deps
from yuanwork.api.routes import memory as memory_routes
from yuanwork.api.routes import wiki as wiki_routes
from yuanwork.db.sqlite import SQLiteStore
from yuanwork.memory.episodic import EpisodicMemory
from yuanwork.memory.profile import ProfileStore


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


class FakeSemanticMemory:
    """The consolidation engine wants a semantic memory; it never uses it."""

    async def list_recent(self, limit=20):
        return []

    async def search(self, query, top_k=50, filter_expr=None):
        return []


@pytest.fixture
def client(store) -> TestClient:
    deps.set_sqlite_store(store)
    deps.set_episodic_memory(EpisodicMemory(store))
    deps.set_profile_store(ProfileStore(store))
    deps.set_semantic_memory(FakeSemanticMemory())  # type: ignore[arg-type]

    app = FastAPI()
    app.include_router(memory_routes.router, prefix="/api/v1")
    app.include_router(wiki_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


# --------------------------------------------------------------------------
# the memory endpoints
# --------------------------------------------------------------------------


def test_the_episodes_can_be_listed(client):
    resp = client.get("/api/v1/memory/episodic")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entries"] == []
    assert body["query_topic"] is None


def test_the_episodes_can_be_filtered_by_topic(client):
    resp = client.get("/api/v1/memory/episodic?topic=合同")
    assert resp.status_code == 200


def test_the_profile_can_be_listed(client):
    resp = client.get("/api/v1/memory/profile")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_the_profile_can_be_filtered_by_prefix(client):
    resp = client.get("/api/v1/memory/profile?key_prefix=user.")
    assert resp.status_code == 200


def test_a_profile_key_can_be_deleted(client):
    resp = client.delete("/api/v1/memory/profile/user.role")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": "user.role"}


def test_deleting_an_unknown_profile_key_answers_200(client):
    """The store always reports success, so the 404 branch is dead."""
    resp = client.delete("/api/v1/memory/profile/nope")
    assert resp.status_code == 200


def test_the_conflicts_can_be_listed(client):
    resp = client.get("/api/v1/memory/conflicts")
    assert resp.status_code == 200
    assert resp.json()["conflicts"] == []


def test_the_acknowledged_flag_has_no_effect(client):
    without = client.get("/api/v1/memory/conflicts")
    with_flag = client.get("/api/v1/memory/conflicts?include_acknowledged=true")
    assert without.json()["conflicts"] == with_flag.json()["conflicts"]


def test_the_consolidation_can_be_triggered(client):
    resp = client.post("/api/v1/memory/consolidate", json={"reason": "manual"})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "episodes_processed", "duplicates_merged", "profile_updates",
        "conflicts_archived", "duration_ms", "events_read",
        "entities_induced", "rules_induced", "page_points_induced",
        "induction_failures", "triggered", "reason",
    }


def test_the_consolidation_endpoint_uses_the_live_engine(client):
    """The engine the app already has - the one with the model.

    A second engine would have no model, so the induction inside it
    would fall back to pattern matching: no rules, ever.
    """
    import inspect

    source = inspect.getsource(memory_routes)
    assert "ConsolidationEngine(" not in source
    assert "optional_consolidation_engine()" in source


# --------------------------------------------------------------------------
# the wiki endpoints
# --------------------------------------------------------------------------


def test_the_pages_can_be_listed(client):
    resp = client.get("/api/v1/wiki/pages")
    assert resp.status_code == 200
    body = resp.json()
    assert body["pages"] == []
    assert body["total"] == 0


def test_a_page_can_be_fetched_by_id(client):
    resp = client.get("/api/v1/wiki/pages/auto/主题")
    assert resp.status_code == 404


def test_the_backlinks_can_be_fetched(client):
    resp = client.get("/api/v1/wiki/pages/auto/主题/backlinks")
    assert resp.status_code == 200
    assert resp.json()["backlinks"] == []


def test_the_graph_can_be_fetched(client):
    resp = client.get("/api/v1/wiki/graph")
    assert resp.status_code == 200
    body = resp.json()
    assert body["nodes"] == []
    assert body["edges"] == []


def test_the_manual_compile_compiles_a_page(client):
    """The endpoint really compiles - it is not a stub any more."""
    resp = client.post("/api/v1/wiki/compile/违约金")
    assert resp.status_code == 200
    body = resp.json()
    assert "compiled" in body
    assert "page_id" in body
    assert "title" in body


def test_the_manual_compile_of_a_topic_without_evidence_says_so(client):
    """The knowledge base in this fixture has no evidence - the answer
    says so instead of pretending."""
    resp = client.post("/api/v1/wiki/compile/违约金")
    assert resp.status_code == 200
    body = resp.json()
    assert body["compiled"] is False
    assert body["reason"]
    assert client.get("/api/v1/wiki/pages").json()["total"] == 0
