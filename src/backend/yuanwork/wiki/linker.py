"""Wiki Linker - manages bidirectional links between pages/chunks/docs/episodes."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore


logger = logging.getLogger(__name__)


class WikiLinker:
    """Manages bidirectional links and backlinks."""

    def __init__(self, sqlite_store: "SQLiteStore"):
        self.sqlite = sqlite_store

    async def link_pages(
        self,
        source_id: str,
        target_id: str,
        link_type: str = "related",
        weight: float = 1.0,
    ) -> None:
        """Create a link between two pages (bidirectional in spirit, single record)."""
        await self.sqlite.add_wiki_link(
            source_id=source_id,
            source_type="page",
            target_id=target_id,
            target_type="page",
            link_type=link_type,
            weight=weight,
        )

    async def link_page_to_chunk(
        self, page_id: str, chunk_id: str, weight: float = 1.0
    ) -> None:
        """Link a page to its source chunk."""
        await self.sqlite.add_wiki_link(
            source_id=page_id,
            source_type="page",
            target_id=chunk_id,
            target_type="chunk",
            link_type="derived_from",
            weight=weight,
        )

    async def link_page_to_doc(
        self, page_id: str, doc_id: str, weight: float = 1.0
    ) -> None:
        """Link a page to its source document."""
        await self.sqlite.add_wiki_link(
            source_id=page_id,
            source_type="page",
            target_id=doc_id,
            target_type="doc",
            link_type="derived_from",
            weight=weight,
        )

    async def link_episode_to_page(
        self, episode_id: int, page_id: str, weight: float = 1.0
    ) -> None:
        """Record that a conversation cited a page."""
        await self.sqlite.add_wiki_link(
            source_id=str(episode_id),
            source_type="episode",
            target_id=page_id,
            target_type="page",
            link_type="cites",
            weight=weight,
        )

    async def get_backlinks(self, page_id: str) -> list[dict[str, Any]]:
        """Get all pages/chunks/episodes that link to this page."""
        return await self.sqlite.get_wiki_backlinks(page_id)

    async def extract_links_from_markdown(
        self, markdown: str
    ) -> list[str]:
        """Extract [[wiki links]] from Markdown content.

        The source markers (`[[来源:文件名 页码]]`) are not links -
        they say where a fact comes from, not which page it refers to.
        """
        links = re.findall(r"\[\[([^\]]+)\]\]", markdown)
        return [link for link in links if not link.startswith("来源:")]

    async def auto_link_page(
        self, page_id: str, markdown: str
    ) -> int:
        """Auto-detect [[links]] in page markdown and create link records.

        Returns count of links created.
        """
        links = await self.extract_links_from_markdown(markdown)
        count = 0
        for link_text in links:
            target_id = self._slugify(link_text)
            if target_id != page_id:  # Don't self-link
                await self.link_pages(
                    source_id=page_id,
                    target_id=target_id,
                    link_type="references",
                )
                count += 1
        return count

    def _slugify(self, text: str) -> str:
        """Generate slug from link text."""
        slug = re.sub(r"[^\w\s-]", "", text.lower())
        slug = re.sub(r"[-\s]+", "-", slug).strip("-")
        return slug[:64] or "untitled"
