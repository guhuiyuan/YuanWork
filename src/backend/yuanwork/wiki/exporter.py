"""Wiki exporter - export wiki pages to static site (Markdown or HTML)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore


logger = logging.getLogger(__name__)


class WikiExporter:
    """Export wiki pages to a static site directory."""

    def __init__(self, sqlite_store: "SQLiteStore"):
        self.sqlite = sqlite_store

    async def export_markdown(self, output_dir: Path) -> int:
        """Export all wiki pages as Markdown files.

        Returns count of pages exported.
        """
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        pages = await self.sqlite.list_wiki_pages(limit=10_000)
        count = 0

        # Build index
        index_lines = ["# YuanWork Wiki Export", "", "## Pages", ""]

        for page in pages:
            page_id = page["id"]
            title = page["title"]

            # Create category subdirectory
            category_dir = output_dir / page["category"]
            category_dir.mkdir(exist_ok=True)

            # Write page
            page_path = category_dir / f"{page_id}.md"
            page_path.write_text(page["markdown"], encoding="utf-8")

            index_lines.append(
                f"- [{title}]({page['category']}/{page_id}.md) "
                f"({page['category']}, {len(page.get('source_chunk_ids', []))} sources)"
            )
            count += 1

        # Write index
        (output_dir / "README.md").write_text(
            "\n".join(index_lines), encoding="utf-8"
        )

        logger.info(f"Exported {count} wiki pages to {output_dir}")
        return count
