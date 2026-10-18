"""Wiki Layer endpoints (Pages, links, entities)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from yuanwork.api.deps import get_sqlite_store
from yuanwork.api.schemas import (
    WikiBacklinks,
    WikiGraphEdge,
    WikiGraphNode,
    WikiGraphResponse,
    WikiLink,
    WikiPageDetail,
    WikiPageInfo,
    WikiPagesResponse,
)
from yuanwork.wiki.graph import WikiGraph

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/wiki/pages", response_model=WikiPagesResponse)
async def list_wiki_pages(
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> WikiPagesResponse:
    """List wiki pages."""
    sqlite = get_sqlite_store()
    pages = await sqlite.list_wiki_pages(
        category=category, limit=limit, offset=offset
    )
    total = await sqlite.count_wiki_pages(category=category)

    return WikiPagesResponse(
        pages=[
            WikiPageInfo(
                id=p["id"],
                title=p["title"],
                summary=p["summary"],
                category=p["category"],
                tags=p.get("tags", []),
                source_doc_ids=p.get("source_doc_ids", []),
                chunk_count=len(p.get("source_chunk_ids", [])),
                version=p["version"],
                created_at=p["created_at"],
                updated_at=p["updated_at"],
                last_compiled_at=p.get("last_compiled_at"),
            )
            for p in pages
        ],
        total=total,
    )


@router.get("/wiki/pages/{page_id:path}/backlinks", response_model=WikiBacklinks)
async def get_wiki_backlinks(page_id: str) -> WikiBacklinks:
    """Get backlinks for a wiki page.

    NOTE: this MUST be registered BEFORE the catch-all
    `/wiki/pages/{page_id:path}` route, otherwise Starlette's path
    converter greedily matches `/backlinks` into the page_id.
    """
    sqlite = get_sqlite_store()
    links = await sqlite.get_wiki_backlinks(page_id)

    return WikiBacklinks(
        page_id=page_id,
        backlinks=[
            WikiLink(
                source_id=l["source_id"],
                source_type=l["source_type"],
                target_id=l["target_id"],
                target_type=l["target_type"],
                link_type=l["link_type"],
                weight=l["weight"],
            )
            for l in links
        ],
    )


@router.get("/wiki/pages/{page_id:path}", response_model=WikiPageDetail)
async def get_wiki_page(page_id: str) -> WikiPageDetail:
    """Get a wiki page by ID (full content)."""
    sqlite = get_sqlite_store()
    page = await sqlite.get_wiki_page(page_id)

    if not page:
        raise HTTPException(status_code=404, detail=f"Wiki page not found: {page_id}")

    return WikiPageDetail(
        id=page["id"],
        title=page["title"],
        summary=page["summary"],
        markdown=page["markdown"],
        category=page["category"],
        tags=page.get("tags", []),
        source_doc_ids=page.get("source_doc_ids", []),
        chunk_count=len(page.get("source_chunk_ids", [])),
        version=page["version"],
        created_at=page["created_at"],
        updated_at=page["updated_at"],
        last_compiled_at=page.get("last_compiled_at"),
    )


@router.get("/wiki/graph", response_model=WikiGraphResponse)
async def get_wiki_graph() -> WikiGraphResponse:
    """Get the full wiki graph (pages + links + entities) for visualization."""
    sqlite = get_sqlite_store()
    graph_engine = WikiGraph(sqlite)
    raw = await graph_engine.build_graph()
    nodes = [
        WikiGraphNode(
            id=n["id"],
            label=n["label"],
            type=n["type"],
            category=n.get("category"),
            weight=n.get("weight", 1) or 1,
        )
        for n in raw.get("nodes", [])
    ]
    edges = [
        WikiGraphEdge(
            source=e["source"],
            target=e["target"],
            type=e["type"],
            weight=e.get("weight", 1.0) or 1.0,
        )
        for e in raw.get("edges", [])
    ]
    return WikiGraphResponse(nodes=nodes, edges=edges)


@router.post("/wiki/compile/{topic:path}")
async def trigger_wiki_compile(topic: str) -> dict:
    """Trigger wiki page compilation for a topic."""
    # TODO: implement wiki page generation
    return {
        "topic": topic,
        "status": "not_implemented",
        "message": "Wiki compilation will be implemented in stage 2",
    }
