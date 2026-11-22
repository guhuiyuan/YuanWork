"""Wiki Layer endpoints (Pages, links, entities)."""

from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query

from yuanwork.api.deps import get_sqlite_store
from yuanwork.api.schemas import (
    WikiBacklinks,
    WikiCompileResponse,
    WikiConclusion,
    WikiConclusionsResponse,
    WikiGraphEdge,
    WikiGraphNode,
    WikiGraphResponse,
    WikiLink,
    WikiPageBlock,
    WikiPageBlocksResponse,
    WikiPageDetail,
    WikiPageEdit,
    WikiPageInfo,
    WikiPagesResponse,
)
from yuanwork.wiki.graph import WikiGraph
from yuanwork.wiki.linker import WikiLinker
from yuanwork.wiki.page_generator import (
    extract_key_points,
    extract_summary,
    render_blocks,
    split_paragraphs,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: How many pieces of evidence a manual compile reads.
MANUAL_COMPILE_TOP_K = 8


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
    stale = await _stale_docs(sqlite)

    return WikiPagesResponse(
        pages=[
            WikiPageInfo(
                id=p["id"],
                title=p["title"],
                summary=p["summary"],
                key_points=p.get("key_points", []),
                category=p["category"],
                tags=p.get("tags", []),
                source_doc_ids=p.get("source_doc_ids", []),
                chunk_count=len(p.get("source_chunk_ids", [])),
                version=p["version"],
                stale=_page_is_stale(p, stale),
                created_at=p["created_at"],
                updated_at=p["updated_at"],
                last_compiled_at=p.get("last_compiled_at"),
            )
            for p in pages
        ],
        total=total,
    )


async def _stale_docs(sqlite) -> set[str]:
    """The documents whose file changed after they were compiled."""
    try:
        return set(await sqlite.list_stale_doc_ids())
    except Exception:  # noqa: BLE001 - the marker is a nicety
        return set()


def _page_is_stale(page: dict[str, Any], stale: set[str]) -> bool:
    """Whether a page was compiled from a file that has changed.

    The page is kept - it is what the document said - but the reader
    has to know that the file in front of him is not the one it was
    read from (AC-E-015).
    """
    return any(
        doc_id in stale for doc_id in (page.get("source_doc_ids") or [])
    )


@router.get("/wiki/pages/{page_id:path}/backlinks", response_model=WikiBacklinks)
async def get_wiki_backlinks(page_id: str) -> WikiBacklinks:
    """Get backlinks for a wiki page.

    The other end of each link comes with its name - a page id is not
    something a reader can use.

    NOTE: this MUST be registered BEFORE the catch-all
    `/wiki/pages/{page_id:path}` route, otherwise Starlette's path
    converter greedily matches `/backlinks` into the page_id.
    """
    sqlite = get_sqlite_store()
    links = await sqlite.get_wiki_backlinks(page_id)

    backlinks = []
    for link in links:
        backlinks.append(
            WikiLink(
                source_id=link["source_id"],
                source_type=link["source_type"],
                target_id=link["target_id"],
                target_type=link["target_type"],
                link_type=link["link_type"],
                weight=link["weight"],
                title=await _other_end_title(sqlite, link),
            )
        )

    return WikiBacklinks(page_id=page_id, backlinks=backlinks)


async def _other_end_title(sqlite, link: dict[str, Any]) -> str:
    """The name of the other end of a link."""
    if link["source_type"] == "page":
        page = await sqlite.get_wiki_page(link["source_id"])
        return page["title"] if page else ""
    if link["source_type"] == "doc":
        file_row = await sqlite.get_file(link["source_id"])
        return file_row["file_name"] if file_row else ""
    return ""


@router.get(
    "/wiki/pages/{page_id:path}/conclusions", response_model=WikiConclusionsResponse
)
async def list_page_conclusions(page_id: str) -> WikiConclusionsResponse:
    """The conclusions of a page, each with where it came from.

    The anchor is what lets the reader jump back to the original: the
    shell opens the file at that position. A conclusion that has no
    anchor is not written down at all - a claim nobody can check cannot
    be used to answer either (ADR-032).

    NOTE: this MUST be registered BEFORE the catch-all
    `/wiki/pages/{page_id:path}` route, otherwise Starlette's path
    converter greedily matches `/conclusions` into the page_id.
    """
    from yuanwork.utils.anchor import anchor_location

    sqlite = get_sqlite_store()
    rows = await sqlite.list_page_conclusions(page_id)

    conclusions = []
    for row in rows:
        anchor = row.get("anchor") or {}
        file_row = await sqlite.get_file(row.get("source_doc_id") or "")
        conclusions.append(
            WikiConclusion(
                id=row["id"],
                page_id=row["page_id"],
                text=row["text"],
                anchor=anchor,
                location=anchor_location(anchor),
                file_name=file_row["file_name"] if file_row else "",
                file_path=file_row["file_path"] if file_row else "",
                segment_index=int(row.get("segment_index") or 0),
                active=bool(row.get("active", 1)),
            )
        )

    return WikiConclusionsResponse(page_id=page_id, conclusions=conclusions)


@router.get(
    "/wiki/pages/{page_id:path}/blocks", response_model=WikiPageBlocksResponse
)
async def list_page_blocks(page_id: str) -> WikiPageBlocksResponse:
    """The blocks of a page - its paragraphs, one by one.

    Every paragraph says who wrote it. A paragraph the user wrote is
    theirs: a later automatic recompile replaces the model's paragraphs
    and leaves this one alone (AC-F-027). The reader has to be able to
    tell the two apart - that is what this endpoint is for.

    NOTE: this MUST be registered BEFORE the catch-all
    `/wiki/pages/{page_id:path}` route, otherwise Starlette's path
    converter greedily matches `/blocks` into the page_id.
    """
    sqlite = get_sqlite_store()
    page = await sqlite.get_wiki_page(page_id)

    if not page:
        raise HTTPException(status_code=404, detail=f"Wiki page not found: {page_id}")

    rows = await sqlite.list_page_blocks(page_id)
    blocks = _page_blocks(rows, split_paragraphs(page["markdown"]))

    return WikiPageBlocksResponse(
        page_id=page_id, blocks=blocks, total=len(blocks)
    )


def _page_blocks(
    rows: list[dict[str, Any]], paragraphs: list[str]
) -> list[WikiPageBlock]:
    """The blocks of a page: its paragraphs, with the owner each has.

    The page body says what the page says now; the table says who
    wrote what. A paragraph the table knows takes its owner's word;
    one the table does not know was written by the model - it is the
    new text of a recompile. A row the body does not have is a
    paragraph the user wrote and a recompile left out: it is still
    theirs, and it still belongs to the page.
    """
    remaining = list(rows)
    blocks: list[WikiPageBlock] = []

    for index, text in enumerate(paragraphs):
        owner = "model"
        for row in remaining:
            if (row.get("text") or "").strip() == text:
                owner = _owner_of(row)
                remaining.remove(row)
                break
        blocks.append(WikiPageBlock(index=index, text=text, owner=owner))

    for row in remaining:
        blocks.append(
            WikiPageBlock(
                index=len(blocks),
                text=(row.get("text") or "").strip(),
                owner=_owner_of(row),
            )
        )

    return blocks


def _owner_of(row: dict[str, Any]) -> Literal["model", "user"]:
    """The owner of a block - "model" or "user", and nothing else."""
    owner = row.get("owner") or "model"
    return owner if owner in ("model", "user") else "model"


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
        key_points=page.get("key_points", []),
        markdown=page["markdown"],
        category=page["category"],
        tags=page.get("tags", []),
        source_doc_ids=page.get("source_doc_ids", []),
        chunk_count=len(page.get("source_chunk_ids", [])),
        version=page["version"],
        stale=_page_is_stale(page, await _stale_docs(sqlite)),
        created_at=page["created_at"],
        updated_at=page["updated_at"],
        last_compiled_at=page.get("last_compiled_at"),
    )


@router.put("/wiki/pages/{page_id:path}", response_model=WikiPageDetail)
async def edit_wiki_page(page_id: str, body: WikiPageEdit) -> WikiPageDetail:
    """The user edits the body of a page.

    The paragraphs the user changed become the user's own: a later
    automatic recompile replaces the model's paragraphs and leaves
    these alone (AC-F-027, AC-F-013).
    """
    sqlite = get_sqlite_store()
    page = await sqlite.get_wiki_page(page_id)

    if not page:
        raise HTTPException(status_code=404, detail=f"Wiki page not found: {page_id}")

    paragraphs = split_paragraphs(body.markdown)
    if not paragraphs:
        raise HTTPException(status_code=400, detail="The page body is empty")

    old_blocks = await sqlite.list_page_blocks(page_id)
    owners = _owners_after_edit(old_blocks, paragraphs)

    blocks = [
        {"text": text, "owner": owner}
        for text, owner in zip(paragraphs, owners)
    ]
    markdown = render_blocks(blocks)

    await sqlite.replace_page_blocks(page_id, blocks)
    await sqlite.update_wiki_page_body(
        page_id,
        markdown=markdown,
        summary=extract_summary(markdown),
        key_points=extract_key_points(markdown),
    )

    # The links in the new text are links now.
    await WikiLinker(sqlite).auto_link_page(page_id, markdown)

    updated = await sqlite.get_wiki_page(page_id)
    return WikiPageDetail(
        id=updated["id"],
        title=updated["title"],
        summary=updated["summary"],
        key_points=updated.get("key_points", []),
        markdown=updated["markdown"],
        category=updated["category"],
        tags=updated.get("tags", []),
        source_doc_ids=updated.get("source_doc_ids", []),
        chunk_count=len(updated.get("source_chunk_ids", [])),
        version=updated["version"],
        created_at=updated["created_at"],
        updated_at=updated["updated_at"],
        last_compiled_at=updated.get("last_compiled_at"),
    )


def _owners_after_edit(
    old_blocks: list[dict[str, Any]], paragraphs: list[str]
) -> list[str]:
    """Who owns each paragraph after the user's edit.

    The paragraphs the user did not touch keep the owner they had -
    only what changed becomes the user's.
    """
    owners = ["user"] * len(paragraphs)
    old_texts = [block.get("text", "") for block in old_blocks]
    if not old_texts:
        return owners

    matcher = SequenceMatcher(a=old_texts, b=paragraphs)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(j2 - j1):
            owners[j1 + offset] = old_blocks[i1 + offset].get("owner", "model")
    return owners


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
            key_points=n.get("key_points", []),
            aliases=n.get("aliases", []),
            related_pages=n.get("related_pages", []),
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


@router.post("/wiki/compile/{topic:path}", response_model=WikiCompileResponse)
async def trigger_wiki_compile(topic: str) -> WikiCompileResponse:
    """Compile a page for a topic, now.

    The automatic trigger compiles a page when the evidence cluster is
    big enough. This one compiles when the user asks for it: the topic
    is searched, the page is written, and its identifier comes back.

    The page it writes is the page the automatic trigger would write -
    a manual compile updates the page instead of piling up a second
    one.
    """
    from yuanwork.api.deps import (
        get_llm_gateway,
        get_semantic_memory,
        get_sqlite_store,
    )
    from yuanwork.retrieval.hybrid import HybridRetriever
    from yuanwork.wiki.page_generator import WikiPageGenerator, slugify

    sqlite = get_sqlite_store()
    semantic = get_semantic_memory()

    try:
        retriever = HybridRetriever(semantic)
        evidence = await retriever.search(topic, top_k=MANUAL_COMPILE_TOP_K)
    except Exception as e:
        # The reason stays in the log - the response says what happened,
        # not what the exception said (ADR-021).
        logger.warning(f"The manual wiki compile could not search: {e}")
        return WikiCompileResponse(
            compiled=False, title=topic, reason="search_failed"
        )

    if not evidence:
        return WikiCompileResponse(
            compiled=False, title=topic, reason="no_evidence"
        )

    generator = WikiPageGenerator(
        sqlite_store=sqlite, llm_gateway=get_llm_gateway()
    )
    try:
        compiled = await generator.compile_page(
            title=topic,
            chunks=evidence,
            page_id=f"auto/{slugify(topic)}",
        )
    except Exception as e:
        logger.warning(f"The manual wiki compile failed: {e}")
        return WikiCompileResponse(
            compiled=False, title=topic, reason="compile_failed"
        )

    return WikiCompileResponse(
        compiled=True,
        page_id=compiled.get("page_id", ""),
        title=compiled.get("title", topic),
        chunk_count=len(evidence),
    )
