"""Wiki graph - entity and page relationship graph."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore


logger = logging.getLogger(__name__)


class WikiGraph:
    """Build a graph of pages, chunks, entities, and documents."""

    def __init__(self, sqlite_store: "SQLiteStore"):
        self.sqlite = sqlite_store

    async def build_graph(self) -> dict[str, Any]:
        """Build the full graph for visualization.

        Returns dict suitable for d3.js or vis-network:
        {
            "nodes": [{"id": ..., "label": ..., "type": ..., "weight": ...}],
            "edges": [{"source": ..., "target": ..., "type": ..., "weight": ...}]
        }
        """
        # Get all pages
        pages = await self.sqlite.list_wiki_pages(limit=500)
        nodes = []
        edges = []

        for page in pages:
            nodes.append({
                "id": page["id"],
                "label": page["title"],
                "type": "page",
                "category": page["category"],
                "weight": page.get("version", 1),
                # The key points are what the page says - the graph
                # view shows them without opening the page.
                "key_points": page.get("key_points", []),
            })

        # Get all wiki links (page-to-page)
        for page in pages:
            outgoing = await self.sqlite.get_wiki_outgoing_links(page["id"])
            for link in outgoing:
                if link["target_type"] == "page":
                    edges.append({
                        "source": link["source_id"],
                        "target": link["target_id"],
                        "type": link["link_type"],
                        "weight": link["weight"],
                    })

        # Add entity nodes
        async with self.sqlite.conn.execute(
            "SELECT id, name, type, mention_count FROM entities ORDER BY mention_count DESC LIMIT 100"
        ) as cursor:
            entity_rows = await cursor.fetchall()

        for row in entity_rows:
            entity_id, name, ent_type, mention_count = row
            nodes.append({
                "id": entity_id,
                "label": name,
                "type": f"entity_{ent_type}",
                "weight": mention_count or 1,
            })

        return {"nodes": nodes, "edges": edges}

    async def get_related_pages(
        self, page_id: str, max_depth: int = 2
    ) -> list[dict[str, Any]]:
        """Get pages related to a given page within N hops."""
        visited = set()
        queue = [(page_id, 0)]
        related: list[dict[str, Any]] = []

        while queue:
            current_id, depth = queue.pop(0)
            if current_id in visited or depth > max_depth:
                continue
            visited.add(current_id)

            if depth > 0:
                page = await self.sqlite.get_wiki_page(current_id)
                if page:
                    related.append({
                        "id": page["id"],
                        "title": page["title"],
                        "depth": depth,
                    })

            # Get outgoing links
            outgoing = await self.sqlite.get_wiki_outgoing_links(current_id)
            for link in outgoing:
                if link["target_type"] == "page":
                    queue.append((link["target_id"], depth + 1))

        return related
