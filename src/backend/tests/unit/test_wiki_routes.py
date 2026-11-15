"""Regression tests for the wiki API route definitions.

These tests catch the FastAPI `:path` converter gotcha: a path-param route
defined before a more specific literal-suffix route will swallow the
literal segment, returning 404 for nested resources.
"""

from __future__ import annotations

from yuanwork.api.routes.wiki import router as wiki_router


def _wiki_routes() -> list[str]:
    """Return the wiki routes' path patterns in registration order."""
    return [r.path for r in wiki_router.routes if hasattr(r, "path")]


def test_routes_register_in_correct_order():
    """The `/backlinks`, `/conclusions` and `/blocks` sub-routes MUST
    come before the catch-all `/wiki/pages/{page_id:path}` route.
    Otherwise the `:path` converter greedily matches `/backlinks` into
    page_id, breaking the backlinks endpoint for any page whose id
    contains a slash (e.g. `auto/foo`).
    """
    assert _wiki_routes() == [
        "/wiki/pages",
        "/wiki/pages/{page_id:path}/backlinks",
        "/wiki/pages/{page_id:path}/conclusions",
        "/wiki/pages/{page_id:path}/blocks",
        "/wiki/pages/{page_id:path}",
        "/wiki/pages/{page_id:path}",
        "/wiki/graph",
        "/wiki/compile/{topic:path}",
    ], f"Wiki routes out of expected order: {_wiki_routes()}"


def test_catch_all_uses_path_converter():
    """Sanity check: the page detail route must use the `:path` converter
    so slashes inside page IDs (e.g. `auto/违约金条款`) match.
    """
    paths = _wiki_routes()
    detail = next(p for p in paths if p == "/wiki/pages/{page_id:path}")
    assert "{page_id:path}" in detail, (
        f"Detail route lost the :path converter: {detail!r}. "
        "Page IDs with slashes (auto/...) will 404 without it."
    )
