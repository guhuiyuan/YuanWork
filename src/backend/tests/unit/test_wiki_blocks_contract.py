"""TASK-050 — the blocks of a page, and who wrote each one.

The page body is shown paragraph by paragraph, and the reader has to
be able to tell the model's paragraphs from the user's own: a
paragraph the user wrote is theirs, and a later automatic recompile
replaces the model's paragraphs and leaves theirs alone (AC-F-027).

The blocks are the page's paragraphs - the same split the edit
endpoint uses - and each of them comes with the owner it has in the
page's own table. Nothing is generated here: the reader is told who
wrote what, and the words are the page's own.

* AC-F-027 — the page's paragraphs come with their owner
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


PAGE_MARKDOWN = (
    "# 违约金条款\n\n"
    "## 摘要\n\n违约金按合同总额的三分之一计算。\n\n"
    "## 要点\n- 违约金上限是合同总额的三分之一\n"
)

PAGE_PARAGRAPHS = [
    "# 违约金条款",
    "## 摘要\n\n违约金按合同总额的三分之一计算。",
    "## 要点\n- 违约金上限是合同总额的三分之一",
]


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def client(store) -> TestClient:
    deps.set_sqlite_store(store)
    app = FastAPI()
    app.include_router(wiki_routes.router, prefix="/api/v1")
    yield TestClient(app)
    deps.reset_dependencies()


async def write_page(
    store,
    page_id: str = "p1",
    markdown: str = PAGE_MARKDOWN,
    blocks: list[dict[str, Any]] | None = None,
) -> None:
    """A page in the store, with the blocks it is made of."""
    await store.upsert_wiki_page(
        page_id=page_id, title="违约金条款", markdown=markdown, summary="摘要",
    )
    if blocks is not None:
        await store.replace_page_blocks(page_id, blocks)


# --------------------------------------------------------------------------
# the blocks of a page
# --------------------------------------------------------------------------


async def test_the_blocks_are_the_paragraphs_of_the_page(client, store):
    """The page's paragraphs, in the order they appear."""
    await write_page(
        store,
        blocks=[{"text": text, "owner": "model"} for text in PAGE_PARAGRAPHS],
    )

    resp = client.get("/api/v1/wiki/pages/p1/blocks")

    assert resp.status_code == 200
    body = resp.json()
    assert body["page_id"] == "p1"
    assert body["total"] == 3
    assert [block["index"] for block in body["blocks"]] == [0, 1, 2]
    assert [block["text"] for block in body["blocks"]] == PAGE_PARAGRAPHS


async def test_the_blocks_say_who_wrote_them(client, store):
    """Every block comes with its owner."""
    await write_page(
        store,
        blocks=[{"text": text, "owner": "model"} for text in PAGE_PARAGRAPHS],
    )

    resp = client.get("/api/v1/wiki/pages/p1/blocks")

    assert [block["owner"] for block in resp.json()["blocks"]] == [
        "model",
        "model",
        "model",
    ]


async def test_a_paragraph_the_user_wrote_is_theirs(client, store):
    """The user's own paragraph says so (AC-F-027)."""
    await write_page(
        store,
        blocks=[
            {"text": PAGE_PARAGRAPHS[0], "owner": "model"},
            {"text": PAGE_PARAGRAPHS[1], "owner": "user"},
            {"text": PAGE_PARAGRAPHS[2], "owner": "model"},
        ],
    )

    resp = client.get("/api/v1/wiki/pages/p1/blocks")

    owners = [block["owner"] for block in resp.json()["blocks"]]
    assert owners == ["model", "user", "model"]
    # The user's paragraph is the one they wrote.
    assert resp.json()["blocks"][1]["text"] == PAGE_PARAGRAPHS[1]


async def test_a_page_that_is_not_there_is_a_404(client):
    """A page that does not exist has no blocks."""
    resp = client.get("/api/v1/wiki/pages/nope/blocks")

    assert resp.status_code == 404


async def test_the_answer_has_the_shape_the_client_expects(client, store):
    """The wire contract: the page, its blocks, and how many."""
    await write_page(
        store,
        blocks=[
            {"text": PAGE_PARAGRAPHS[0], "owner": "model"},
            {"text": PAGE_PARAGRAPHS[1], "owner": "user"},
        ],
    )

    body = client.get("/api/v1/wiki/pages/p1/blocks").json()

    assert set(body) == {"page_id", "blocks", "total"}
    assert isinstance(body["page_id"], str)
    assert isinstance(body["total"], int)
    for block in body["blocks"]:
        assert set(block) == {"index", "text", "owner"}
        assert isinstance(block["index"], int)
        assert isinstance(block["text"], str)
        assert block["owner"] in ("model", "user")


async def test_the_blocks_of_a_page_whose_id_has_a_slash(client, store):
    """The `:path` converter - an id like `auto/违约金条款` is one page."""
    await write_page(
        store,
        page_id="auto/违约金条款",
        blocks=[{"text": text, "owner": "model"} for text in PAGE_PARAGRAPHS],
    )

    resp = client.get("/api/v1/wiki/pages/auto/违约金条款/blocks")

    assert resp.status_code == 200
    body = resp.json()
    assert body["page_id"] == "auto/违约金条款"
    assert body["total"] == 3


async def test_a_page_without_blocks_still_has_its_paragraphs(client, store):
    """A page written before the blocks were kept is still readable."""
    await write_page(store)

    resp = client.get("/api/v1/wiki/pages/p1/blocks")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 3
    assert [block["text"] for block in body["blocks"]] == PAGE_PARAGRAPHS
    assert [block["owner"] for block in body["blocks"]] == [
        "model",
        "model",
        "model",
    ]


async def test_the_blocks_are_the_paragraphs_the_edit_endpoint_makes(client, store):
    """The edit endpoint and the blocks endpoint split the page the
    same way - what the user wrote is the user's own paragraph."""
    await write_page(
        store,
        blocks=[{"text": text, "owner": "model"} for text in PAGE_PARAGRAPHS],
    )

    client.put(
        "/api/v1/wiki/pages/p1",
        json={
            "markdown": (
                "# 违约金条款\n\n## 摘要\n\n我改过的摘要。\n\n"
                "## 要点\n- 违约金上限是合同总额的三分之一\n"
            )
        },
    )

    body = client.get("/api/v1/wiki/pages/p1/blocks").json()
    owners = [block["owner"] for block in body["blocks"]]
    assert owners == ["model", "user", "model"]
    # Three paragraphs - the count is the number of paragraphs the page
    # has, not a copy of the list.
    assert body["total"] == 3


async def test_a_paragraph_the_user_wrote_keeps_its_owner_after_a_recompile(
    client, store
):
    """A recompile replaces the model's paragraphs - not the user's."""
    await write_page(
        store,
        blocks=[
            {"text": PAGE_PARAGRAPHS[0], "owner": "model"},
            {"text": "## 摘要\n\n我改过的摘要。", "owner": "user"},
            {"text": PAGE_PARAGRAPHS[2], "owner": "model"},
        ],
        markdown=(
            "# 违约金条款\n\n## 摘要\n\n我改过的摘要。\n\n"
            "## 要点\n- 违约金上限是合同总额的三分之一\n"
        ),
    )

    body = client.get("/api/v1/wiki/pages/p1/blocks").json()

    assert body["blocks"][1]["owner"] == "user"
    assert body["blocks"][1]["text"] == "## 摘要\n\n我改过的摘要。"


async def test_a_paragraph_the_user_wrote_that_the_body_lost_is_still_theirs(
    client, store
):
    """A recompile leaves the user's paragraph out of the body - it is
    still the user's, and it is still listed."""
    await write_page(
        store,
        blocks=[
            {"text": PAGE_PARAGRAPHS[0], "owner": "model"},
            {"text": "我改过的摘要。", "owner": "user"},
        ],
        markdown=(
            "# 违约金条款\n\n## 摘要\n\n模型写的新摘要。\n\n"
            "## 要点\n- 违约金上限是合同总额的三分之一\n"
        ),
    )

    body = client.get("/api/v1/wiki/pages/p1/blocks").json()

    owners = [block["owner"] for block in body["blocks"]]
    # The body is the model's - the paragraph the user wrote is the
    # last one, and it is theirs.
    assert owners == ["model", "model", "model", "user"]
    assert body["blocks"][-1]["text"] == "我改过的摘要。"
    assert body["total"] == 4
