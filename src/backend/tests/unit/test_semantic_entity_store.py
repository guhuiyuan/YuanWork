"""The entities and the relations between them.

An entity is a thing the knowledge base talks about - a person, an
organisation, a project, a concept, a contract, a law, a place, or
something that does not fit the other seven. It carries the events it
was induced from, so a conclusion can be traced back to what happened,
and a status, so an unconfirmed conclusion does not count as evidence.

* AC-F-015 - the graph can be browsed
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from yuanwork.db.sqlite import SQLiteStore


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


# --------------------------------------------------------------------------
# the categories
# --------------------------------------------------------------------------


CATEGORIES = (
    "person", "org", "project", "concept", "contract", "law", "location", "other",
)


@pytest.mark.parametrize("category", CATEGORIES)
async def test_every_category_can_be_stored(store, category):
    await store.upsert_entity(
        entity_id=f"e-{category}", name=category, entity_type=category
    )

    stored = await store.get_entity(f"e-{category}")
    assert stored is not None
    assert stored["type"] == category


async def test_a_category_outside_the_whitelist_is_refused(store):
    """The eight categories are the whole list - an alien one is a
    mistake, not a new kind of thing."""
    with pytest.raises(sqlite3.IntegrityError):
        await store.conn.execute(
            "INSERT INTO entities (id, name, type) VALUES ('x', 'x', 'alien')"
        )


# --------------------------------------------------------------------------
# what an entity carries
# --------------------------------------------------------------------------


async def test_an_entity_carries_its_aliases_and_properties(store):
    await store.upsert_entity(
        entity_id="e1",
        name="张三",
        entity_type="person",
        aliases=["老张", "张工"],
        properties={"部门": "财务"},
    )

    stored = await store.get_entity("e1")
    assert stored["aliases"] == ["老张", "张工"]
    assert stored["properties"] == {"部门": "财务"}


async def test_an_entity_carries_the_events_it_came_from(store):
    """A conclusion that cannot be traced back is a guess."""
    await store.upsert_entity(
        entity_id="e1",
        name="报销标准",
        entity_type="concept",
        source_event_ids=[3, 7],
    )

    stored = await store.get_entity("e1")
    assert stored["source_event_ids"] == [3, 7]


async def test_an_entity_counts_its_mentions(store):
    await store.upsert_entity(entity_id="e1", name="x", entity_type="concept")
    await store.upsert_entity(entity_id="e1", name="x", entity_type="concept")

    stored = await store.get_entity("e1")
    assert stored["mention_count"] == 2


async def test_an_entity_is_in_effect_by_default(store):
    await store.upsert_entity(entity_id="e1", name="x", entity_type="concept")

    stored = await store.get_entity("e1")
    assert stored["status"] == "active"


async def test_an_entity_can_wait_for_the_user_to_confirm(store):
    """A rule that was induced but not confirmed does not count as
    evidence."""
    await store.upsert_entity(entity_id="e1", name="x", entity_type="concept")
    await store.set_entity_status("e1", "pending")

    stored = await store.get_entity("e1")
    assert stored["status"] == "pending"


async def test_a_pending_entity_is_not_counted_as_evidence(store):
    await store.upsert_entity(entity_id="e1", name="x", entity_type="concept")
    await store.upsert_entity(entity_id="e2", name="y", entity_type="concept")
    await store.set_entity_status("e2", "pending")

    active = await store.list_entities(status="active")
    pending = await store.list_entities(status="pending")

    assert [e["id"] for e in active] == ["e1"]
    assert [e["id"] for e in pending] == ["e2"]


async def test_an_unknown_status_is_refused(store):
    with pytest.raises(ValueError):
        await store.set_entity_status("e1", "maybe")


async def test_the_entities_can_be_listed_by_category(store):
    await store.upsert_entity(entity_id="e1", name="a", entity_type="person")
    await store.upsert_entity(entity_id="e2", name="b", entity_type="contract")

    people = await store.list_entities(entity_type="person")

    assert [e["id"] for e in people] == ["e1"]


# --------------------------------------------------------------------------
# the relations
# --------------------------------------------------------------------------


async def test_a_relation_is_stored(store):
    await store.upsert_entity(entity_id="e1", name="员工", entity_type="person")
    await store.upsert_entity(entity_id="e2", name="公司", entity_type="org")

    relation_id = await store.add_entity_relation(
        source_id="e1", target_id="e2", relation_type="belongs_to"
    )

    assert relation_id > 0
    relations = await store.list_entity_relations()
    assert len(relations) == 1
    assert relations[0]["source_id"] == "e1"
    assert relations[0]["target_id"] == "e2"
    assert relations[0]["relation_type"] == "belongs_to"


async def test_a_relation_carries_the_events_it_came_from(store):
    await store.upsert_entity(entity_id="e1", name="员工", entity_type="person")
    await store.upsert_entity(entity_id="e2", name="公司", entity_type="org")

    await store.add_entity_relation(
        source_id="e1",
        target_id="e2",
        relation_type="belongs_to",
        source_event_ids=[11],
    )

    relations = await store.list_entity_relations()
    assert relations[0]["source_event_ids"] == [11]


async def test_the_same_relation_is_not_stored_twice(store):
    await store.upsert_entity(entity_id="e1", name="员工", entity_type="person")
    await store.upsert_entity(entity_id="e2", name="公司", entity_type="org")

    await store.add_entity_relation(
        source_id="e1", target_id="e2", relation_type="belongs_to"
    )
    await store.add_entity_relation(
        source_id="e1", target_id="e2", relation_type="belongs_to"
    )

    assert len(await store.list_entity_relations()) == 1


async def test_the_relations_of_one_entity_are_found(store):
    await store.upsert_entity(entity_id="e1", name="员工", entity_type="person")
    await store.upsert_entity(entity_id="e2", name="公司", entity_type="org")
    await store.upsert_entity(entity_id="e3", name="合同", entity_type="contract")

    await store.add_entity_relation(
        source_id="e1", target_id="e2", relation_type="belongs_to"
    )
    await store.add_entity_relation(
        source_id="e1", target_id="e3", relation_type="signed"
    )

    relations = await store.list_entity_relations(entity_id="e1")

    assert len(relations) == 2
    assert {r["target_id"] for r in relations} == {"e2", "e3"}


async def test_the_relations_of_an_entity_come_from_both_sides(store):
    await store.upsert_entity(entity_id="e1", name="员工", entity_type="person")
    await store.upsert_entity(entity_id="e2", name="公司", entity_type="org")

    await store.add_entity_relation(
        source_id="e1", target_id="e2", relation_type="belongs_to"
    )

    assert len(await store.list_entity_relations(entity_id="e2")) == 1


async def test_forgetting_an_entity_forgets_its_relations(store):
    await store.upsert_entity(entity_id="e1", name="员工", entity_type="person")
    await store.upsert_entity(entity_id="e2", name="公司", entity_type="org")
    await store.add_entity_relation(
        source_id="e1", target_id="e2", relation_type="belongs_to"
    )

    await store.delete_entity("e1")

    assert await store.get_entity("e1") is None
    assert await store.list_entity_relations() == []


# --------------------------------------------------------------------------
# the mentions
# --------------------------------------------------------------------------


async def test_a_mention_is_recorded(store):
    await store.upsert_entity(entity_id="e1", name="员工", entity_type="person")

    await store.add_entity_mention(
        entity_id="e1", chunk_id="c1", doc_id="d1", context="员工张三负责报销"
    )

    async with store.conn.execute(
        "SELECT entity_id, page_id, chunk_id, doc_id, context FROM entity_mentions "
        "WHERE entity_id = ?",
        ("e1",),
    ) as cursor:
        rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "e1"
    assert rows[0][2] == "c1"
    assert rows[0][4] == "员工张三负责报销"


# --------------------------------------------------------------------------
# an older database
# --------------------------------------------------------------------------


async def test_an_older_database_gains_the_new_columns(tmp_path: Path):
    """A database created before the columns existed keeps working."""
    path = tmp_path / "old.db"

    old = sqlite3.connect(path)
    old.execute(
        """
        CREATE TABLE entities (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            aliases TEXT DEFAULT '[]',
            properties TEXT DEFAULT '{}',
            mention_count INTEGER DEFAULT 0,
            first_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    old.execute(
        "INSERT INTO entities (id, name, type) VALUES ('old', '旧实体', 'concept')"
    )
    old.commit()
    old.close()

    store = SQLiteStore(path)
    try:
        await store.initialize()

        stored = await store.get_entity("old")
        assert stored is not None
        assert stored["status"] == "active"
        assert stored["source_event_ids"] == []
    finally:
        await store.close()
