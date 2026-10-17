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

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore


logger = logging.getLogger(__name__)


WIKI_PAGE_PROMPT = """\
你是一个 Wiki 页面生成助手。把以下相关的文档片段编译成结构化的 Wiki 页面。

## 要求

1. **结构化**：使用 Markdown 标题（##、###）组织内容
2. **保留来源**：每个事实后用 [[来源:文件名 页码/slide]] 标注
3. **生成摘要**：200 字以内的概述（用于列表展示）
4. **提取分类**：从 [project|concept|person|contract|law|general] 中选一个
5. **生成标签**：3-5 个关键词标签
6. **双向链接**：在文末列出"相关页面/概念"（如 [[概念名]]）

## 输出格式（Markdown）

```markdown
# {标题}

> 来源：{doc1, doc2, ...}（共 N 个片段）
> 分类：{category}
> 标签：{tag1, tag2, tag3}
> 最后更新：{now}

## 摘要
{200 字以内}

## 核心内容

### {子章节 1}
{内容} [[来源:文件名 页码/slide]]

### {子章节 2}
{内容} [[来源:文件名 页码/slide]]

## 相关页面/概念
- [[概念名 1]]
- [[概念名 2]]

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

        # Extract summary, tags from generated markdown
        summary = self._extract_summary(markdown)
        tags = self._extract_tags_from_md(markdown)

        # Get source doc IDs
        source_doc_ids = list({c.get("doc_id", "") for c in chunks if c.get("doc_id")})
        source_chunk_ids = [c.get("id", "") for c in chunks if c.get("id")]

        # Save to SQLite
        await self.sqlite.upsert_wiki_page(
            page_id=page_id,
            title=title,
            markdown=markdown,
            summary=summary,
            source_chunk_ids=source_chunk_ids,
            source_doc_ids=source_doc_ids,
            category=category,
            tags=tags,
        )

        # Establish bidirectional links: page → chunks (derived_from)
        for chunk_id in source_chunk_ids:
            if chunk_id:
                await self.sqlite.add_wiki_link(
                    source_id=page_id,
                    source_type="page",
                    target_id=chunk_id,
                    target_type="chunk",
                    link_type="derived_from",
                )

        logger.info(f"Compiled Wiki page: {page_id} ({len(chunks)} chunks)")

        return {
            "page_id": page_id,
            "title": title,
            "markdown": markdown,
            "summary": summary,
            "category": category,
            "tags": tags,
            "source_doc_ids": source_doc_ids,
            "source_chunk_ids": source_chunk_ids,
        }

    async def _generate_with_llm(self, title: str, chunks_text: str) -> str:
        """Generate Markdown using LLM."""
        prompt = WIKI_PAGE_PROMPT.format(
            title=title,
            chunks_text=chunks_text,
            now=datetime.now().strftime("%Y-%m-%d"),
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
        """Simple fallback compilation without LLM."""
        lines = [
            f"# {title}",
            "",
            f"> 自动编译于 {datetime.now().strftime('%Y-%m-%d')}",
            f"> 来源片段数：{len(chunks)}",
            "",
            "## 摘要",
            f"本页基于 {len(chunks)} 个文档片段自动汇编。",
            "",
            "## 核心内容",
            "",
        ]
        for i, chunk in enumerate(chunks, 1):
            meta = chunk.get("meta", {}) or {}
            location = ""
            if meta.get("slide_number"):
                location = f"（Slide {meta['slide_number']}）"
            elif meta.get("page"):
                location = f"（第 {meta['page']} 页）"

            text = chunk.get("text", "")[:500]
            lines.append(f"### {chunk.get('file_name', 'unknown')} {location}")
            lines.append(text)
            lines.append("")

        return "\n".join(lines)

    def _format_chunks_for_prompt(self, chunks: list[dict[str, Any]]) -> str:
        """Format chunks for LLM prompt."""
        lines = []
        for i, chunk in enumerate(chunks, 1):
            meta = chunk.get("meta", {}) or {}
            location = ""
            if meta.get("slide_number"):
                location = f" (Slide {meta['slide_number']})"
            elif meta.get("page"):
                location = f" (第 {meta['page']} 页)"

            lines.append(f"--- 片段 {i} ---")
            lines.append(f"来源：《{chunk.get('file_name', 'unknown')}》{location}")
            lines.append(f"内容：")
            lines.append(chunk.get("text", "")[:1000])
            lines.append("")
        return "\n".join(lines)

    def _extract_summary(self, markdown: str) -> str:
        """Extract the summary section from generated markdown."""
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

    def _extract_tags_from_md(self, markdown: str) -> list[str]:
        """Extract tags from the markdown frontmatter-like header."""
        # Look for "标签：tag1, tag2, tag3" pattern
        match = re.search(r"标签[：:]\s*([^\n]+)", markdown)
        if match:
            tags_str = match.group(1)
            tags = [t.strip() for t in re.split(r"[,，、]", tags_str) if t.strip()]
            return tags[:10]
        return []

    def _slugify(self, text: str) -> str:
        """Generate a URL-safe slug from text."""
        # Simple Chinese-aware slugifier
        slug = re.sub(r"[^\w\s-]", "", text.lower())
        slug = re.sub(r"[-\s]+", "-", slug).strip("-")
        return slug[:64] or "untitled"
