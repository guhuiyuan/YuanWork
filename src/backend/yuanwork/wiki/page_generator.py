"""Wiki Page Generator - compiles chunks into structured Markdown pages.

Stage 1: on-demand compilation (when query triggers it)
Stage 2: background proactive compilation via Consolidation
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

from jinja2 import Environment
from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask
from yuanwork.utils.chunk_meta import chunk_location, chunk_meta
from yuanwork.wiki.linker import WikiLinker

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore


logger = logging.getLogger(__name__)


def slugify(text: str) -> str:
    """A URL-safe identifier for a title.

    The automatic trigger and the manual compile have to agree on it -
    otherwise a manual compile writes a second page instead of updating
    the one that is there.
    """
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    return slug[:64] or "untitled"


def split_paragraphs(markdown: str) -> list[str]:
    """The blocks of a page.

    A heading and the text under it belong together - the user edits
    the text, not the heading above it. Text without a heading is cut
    into paragraphs.
    """
    blocks: list[str] = []
    current: list[str] = []
    has_heading = False

    for line in (markdown or "").splitlines():
        if _is_heading(line):
            if current and "\n".join(current).strip():
                blocks.append("\n".join(current).strip())
            current = [line]
            has_heading = True
            continue
        if not current:
            current = [line]
            continue
        if not line.strip() and not has_heading:
            # A break between two paragraphs of plain text.
            blocks.append("\n".join(current).strip())
            current = []
            has_heading = False
            continue
        current.append(line)

    if current and "\n".join(current).strip():
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def _is_heading(line: str) -> bool:
    """Whether a line is a markdown heading."""
    return bool(re.match(r"^#{1,6}\s+\S", line))


def render_blocks(blocks: list[dict[str, Any]]) -> str:
    """The text of a page, from its blocks."""
    return "\n\n".join(block.get("text", "") for block in blocks)


def merge_blocks(
    existing: list[dict[str, Any]], new_texts: list[str]
) -> list[dict[str, Any]]:
    """The blocks of a page after a recompile (ADR-037).

    The user's own paragraphs stay where they are; the model's
    paragraphs are the new ones. A page the user never touched is
    simply the new compilation.
    """
    if not any(block.get("owner") == "user" for block in existing):
        return [{"text": text, "owner": "model"} for text in new_texts]

    merged: list[dict[str, Any]] = []
    for index, block in enumerate(existing):
        if block.get("owner") == "user":
            # The user's paragraph is theirs - it stays, and the
            # model's new text for that place is dropped.
            merged.append(
                {
                    "id": block.get("id"),
                    "text": block.get("text", ""),
                    "owner": "user",
                    "conclusion_id": block.get("conclusion_id"),
                }
            )
        elif index < len(new_texts):
            merged.append({"text": new_texts[index], "owner": "model"})
    # Whatever the new compilation has beyond the old page goes to the
    # end - a section the old page did not have.
    for text in new_texts[len(existing):]:
        merged.append({"text": text, "owner": "model"})
    return merged


def extract_summary(markdown: str) -> str:
    """The summary of a page - the 摘要 section, or the first lines."""
    match = re.search(
        r"##\s*摘要\s*\n+(.*?)(?=\n##|\Z)",
        markdown,
        re.DOTALL,
    )
    if match:
        summary = match.group(1).strip()
        return summary[:300]
    # Fallback: first 200 chars after title
    lines = markdown.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return " ".join(lines[i + 1 : i + 5])[:200]
    return markdown[:200]


def extract_key_points(markdown: str) -> list[str]:
    """The key points of a page.

    They are the bullets of the 要点 section. A model that wrote them
    somewhere else still has them - the first bullets of the page are
    its points.
    """
    points = _bullets_of_section(markdown, ("要点", "关键要点", "核心要点"))
    if not points:
        points = _bullets(markdown)
    return [point for point in points if point][:10]


def extract_tags(markdown: str) -> list[str]:
    """The tags of a page - the 标签 line in its header."""
    match = re.search(r"标签[：:]\s*([^\n]+)", markdown)
    if match:
        tags_str = match.group(1)
        tags = [t.strip() for t in re.split(r"[,，、]", tags_str) if t.strip()]
        return tags[:10]
    return []


def _bullets_of_section(markdown: str, names: tuple[str, ...]) -> list[str]:
    """The bullets of the first section that is there."""
    for name in names:
        match = re.search(
            r"##\s*" + re.escape(name) + r"\s*\n+(.*?)(?=\n##|\Z)",
            markdown,
            re.DOTALL,
        )
        if not match:
            continue
        bullets = _bullets(match.group(1))
        if bullets:
            return bullets
        # The section is there but its points are not bullets - take
        # its lines instead.
        lines = [
            line.strip()
            for line in match.group(1).splitlines()
            if line.strip() and not line.strip().startswith(("#", ">", "-", "*"))
        ]
        if lines:
            return lines
    return []


def _bullets(text: str) -> list[str]:
    """The list items of a text."""
    points: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(("- ", "* ", "+ ")):
            points.append(line[2:].strip())
        elif re.match(r"^\d+[.)]\s", line):
            points.append(re.sub(r"^\d+[.)]\s*", "", line).strip())
    return points


WIKI_PAGE_PROMPT = """\
你是一个 Wiki 页面生成助手。把以下相关的文档片段编译成结构化的 Wiki 页面。

## 要求

1. **四个部分**：标题、摘要、要点、出处 —— 一个不少，顺序照下面的格式
2. **保留来源**：每条要点后用 [[来源:文件名 页码/slide]] 标注
3. **生成摘要**：200 字以内的概述（用于列表展示）
4. **提取分类**：从 [project|concept|person|contract|law|general] 中选一个
5. **生成标签**：3-5 个关键词标签
6. **要点是列表**：每条一行，以 "- " 开头，一条一个事实

## 输出格式（Markdown）

```markdown
# {{标题}}

> 分类：{{category}}
> 标签：{{tag1, tag2, tag3}}
> 最后更新：{{date_placeholder}}

## 摘要
{{200 字以内}}

## 要点
- {{要点 1}} [[来源:文件名 页码/slide]]
- {{要点 2}} [[来源:文件名 页码/slide]]
- {{要点 3}} [[来源:文件名 页码/slide]]

## 出处
[1] 《文件 1》第 N 页
[2] 《文件 2》Slide N
```

## 相关文档片段

{chunks_text}

请生成 Wiki 页面（只输出 Markdown 内容）：
"""


class WikiPageGenerator:
    """Generates Wiki pages from chunks."""

    def __init__(
        self,
        sqlite_store: "SQLiteStore",
        llm_gateway: LLMGateway | None = None,
    ):
        self.sqlite = sqlite_store
        self.llm = llm_gateway
        self.linker = WikiLinker(sqlite_store)

    async def compile_page(
        self,
        title: str,
        chunks: list[dict[str, Any]],
        category: str = "general",
        page_id: str | None = None,
    ) -> dict[str, Any]:
        """Compile a Wiki page from chunks.

        Args:
            title: Page title (used to generate page_id if not provided)
            chunks: List of chunk dicts with text, file_name, meta
            category: Page category
            page_id: Optional page ID (defaults to slugified title)

        Returns:
            Dict with page_id, title, markdown, summary
        """
        if not chunks:
            return {"error": "no_chunks"}

        if page_id is None:
            page_id = self._slugify(title)

        # Build chunks text for prompt
        chunks_text = self._format_chunks_for_prompt(chunks)

        # Generate Markdown
        if self.llm is not None:
            markdown = await self._generate_with_llm(title, chunks_text)
        else:
            markdown = self._fallback_compile(title, chunks)

        # Post-process: substitute the date placeholder that the LLM
        # copies verbatim from the example output format.
        markdown = markdown.replace(
            "{date_placeholder}", datetime.now().strftime("%Y-%m-%d")
        )

        # The page is made of blocks, one paragraph each. A recompile
        # replaces the model's blocks and leaves the user's own alone
        # (AC-F-027).
        blocks = await self._merge_blocks(page_id, split_paragraphs(markdown))
        markdown = render_blocks(blocks)

        # Extract the four parts of the page from the page as it now
        # stands - the user may have changed it.
        summary = self._extract_summary(markdown)
        key_points = self._extract_key_points(markdown)
        tags = self._extract_tags_from_md(markdown)

        # Get source doc IDs
        source_doc_ids = list({c.get("doc_id", "") for c in chunks if c.get("doc_id")})
        source_chunk_ids = [c.get("id", "") for c in chunks if c.get("id")]

        # Save to SQLite
        await self.sqlite.replace_page_blocks(page_id, blocks)
        await self.sqlite.upsert_wiki_page(
            page_id=page_id,
            title=title,
            markdown=markdown,
            summary=summary,
            key_points=key_points,
            source_chunk_ids=source_chunk_ids,
            source_doc_ids=source_doc_ids,
            category=category,
            tags=tags,
            user_edited=any(b.get("owner") == "user" for b in blocks),
        )

        # Establish the links (AC-F-014): the page points at the
        # documents it was compiled from, at the slices it was compiled
        # from, and at the pages it mentions.
        for chunk_id in source_chunk_ids:
            if chunk_id:
                await self.linker.link_page_to_chunk(page_id, chunk_id)
        for doc_id in source_doc_ids:
            if doc_id:
                await self.linker.link_page_to_doc(page_id, doc_id)
        await self.linker.auto_link_page(page_id, markdown)

        logger.info(f"Compiled Wiki page: {page_id} ({len(chunks)} chunks)")

        return {
            "page_id": page_id,
            "title": title,
            "markdown": markdown,
            "summary": summary,
            "key_points": key_points,
            "category": category,
            "tags": tags,
            "source_doc_ids": source_doc_ids,
            "source_chunk_ids": source_chunk_ids,
        }

    async def _merge_blocks(
        self, page_id: str, new_blocks: list[str]
    ) -> list[dict[str, Any]]:
        """The blocks of the page after a recompile.

        The user's own paragraphs stay where they are; the model's
        paragraphs are the new ones (AC-F-027). A page the user never
        touched is simply the new compilation.
        """
        existing = await self.sqlite.list_page_blocks(page_id)
        return merge_blocks(existing, new_blocks)

    async def _generate_with_llm(self, title: str, chunks_text: str) -> str:
        """Generate Markdown using LLM."""
        prompt = WIKI_PAGE_PROMPT.format(
            title=title,
            chunks_text=chunks_text,
            date_placeholder=datetime.now().strftime("%Y-%m-%d"),
        )

        task = LLMTask(
            task_type="wiki_compile",
            messages=[LLMMessage(role="user", content=prompt)],
            temperature=0.1,
            max_tokens=3000,
        )

        try:
            response = await self.llm.complete(task)
            content = response.content.strip()
            # Strip code fences if present
            if content.startswith("```"):
                lines = content.split("\n")
                # Find first and last fence
                start = 1 if lines[0].startswith("```") else 0
                end = len(lines) - 1 if lines[-1].startswith("```") else len(lines)
                content = "\n".join(lines[start:end])
            return content
        except Exception as e:
            logger.warning(f"Wiki page LLM generation failed: {e}")
            return self._fallback_compile(title, [])

    def _fallback_compile(
        self, title: str, chunks: list[dict[str, Any]]
    ) -> str:
        """Simple fallback compilation without LLM.

        The page still has the four parts - a page without key points
        is not a page the reader can use.
        """
        lines = [
            f"# {title}",
            "",
            f"> 自动编译于 {datetime.now().strftime('%Y-%m-%d')}",
            f"> 来源片段数：{len(chunks)}",
            "",
            "## 摘要",
            f"本页基于 {len(chunks)} 个文档片段自动汇编。",
            "",
            "## 要点",
            "",
        ]
        for chunk in chunks:
            where = chunk_location(chunk)
            text = chunk.get("text", "")[:200].strip()
            marker = (
                f" [[来源:{chunk.get('file_name', 'unknown')} {where}]]"
                if where
                else ""
            )
            lines.append(f"- {text}{marker}".rstrip())

        lines += ["", "## 出处", ""]
        for i, chunk in enumerate(chunks, 1):
            where = chunk_location(chunk)
            location = f" {where}" if where else ""
            lines.append(f"[{i}] 《{chunk.get('file_name', 'unknown')}》{location}")

        return "\n".join(lines)

    def _format_chunks_for_prompt(self, chunks: list[dict[str, Any]]) -> str:
        """Format chunks for LLM prompt."""
        lines = []
        for i, chunk in enumerate(chunks, 1):
            where = chunk_location(chunk)
            location = f" ({where})" if where else ""

            lines.append(f"--- 片段 {i} ---")
            lines.append(f"来源：《{chunk.get('file_name', 'unknown')}》{location}")
            lines.append(f"内容：")
            lines.append(chunk.get("text", "")[:1000])
            lines.append("")
        return "\n".join(lines)

    def _extract_summary(self, markdown: str) -> str:
        """Extract the summary section from generated markdown."""
        return extract_summary(markdown)

    def _extract_key_points(self, markdown: str) -> list[str]:
        """The key points of the page."""
        return extract_key_points(markdown)

    def _extract_tags_from_md(self, markdown: str) -> list[str]:
        """Extract tags from the markdown frontmatter-like header."""
        return extract_tags(markdown)

    def _slugify(self, text: str) -> str:
        """Generate a URL-safe slug from text."""
        return slugify(text)
