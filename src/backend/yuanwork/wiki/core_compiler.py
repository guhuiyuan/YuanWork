"""Compiling a core document into pages, at ingest time.

A core document is one that other documents are read against: a
contract, a rule, a statute, a company's own policy. It is read in
full when it comes in, and what it says is written down as pages -
an entity page (the parties and the things the document defines), a
concept page (the terms it introduces), or a comparison table (when
the document revises an earlier version).

The compilation happens while the document is being indexed, not when
a question comes in: the pages are there before anybody asks (ADR-031).

Every conclusion is written down with the place it was read from
(ADR-032), and when a revised document contradicts what the page said
before, both versions are kept and the difference is put side by side
(ADR-033) - a policy that changed behind the user's back is worse than
one that was never written down.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def _short_hash(text: str) -> str:
    """A short, stable identifier for a piece of text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


#: The three kinds of page a core document can produce.
PAGE_TYPE_ENTITY = "entity"
PAGE_TYPE_CONCEPT = "concept"
PAGE_TYPE_COMPARISON = "comparison"

#: The layer the compiled pages belong to.
CORE_TIER = "core"

#: How long one compilation may take (AC-P-008: five minutes, without
#: the queue in front of the model).
DEFAULT_COMPILE_TIMEOUT = 240.0

#: How much of the document the model reads in one go.
DEFAULT_SEGMENT_CHARS = 40_000

#: How much of a document one read may hold. A document longer than
#: this is read in several goes, and every conclusion says which part
#: it came from (AC-E-013).
DEFAULT_READ_CHARS = 60_000


def default_read_chars() -> int:
    """How much of a document one read may hold, from the settings."""
    try:
        from yuanwork.config import get_settings

        settings = get_settings()
        return int(settings.core_read_chars)
    except Exception:  # noqa: BLE001 - the default is enough
        return DEFAULT_READ_CHARS

COMPILE_PROMPT = """\
你是一个文档编译器。下面是一份「核心层文档」——别的文档要拿它当依据的那种
（合同、规章、法条、公司内部制度与流程规范）。

请通读全文，产出下面三类页面中**至少一类**：

1. **实体页**：这份文档定义了哪些主体与对象（公司、人、部门、项目、合同标的），
   它们之间是什么关系。
2. **概念页**：这份文档引入了哪些概念、术语、规则，它们怎么定义、怎么适用。
3. **对比表**：这份文档是否修订了某个已有版本；有的话，把新旧两版的说法并排列出。

只输出 JSON，格式如下：

{{
  "entity_pages": [
    {{
      "title": "页面标题",
      "summary": "一句话说明这页讲什么",
      "key_points": ["要点一", "要点二"],
      "conclusions": [
        {{"text": "一条结论", "segment": 0}}
      ]
    }}
  ],
  "concept_pages": [
    {{"title": "...", "summary": "...", "key_points": ["..."], "conclusions": [{{"text": "...", "segment": 0}}]}}
  ],
  "comparison_tables": [
    {{
      "title": "...",
      "summary": "...",
      "key_points": ["旧版：...", "新版：..."],
      "conclusions": [{{"text": "...", "segment": 0}}]
    }}
  ]
}}

要求：
- `conclusions` 里的每一条都是文档里**写明**的结论，不是你的推断。
- 每条结论都要写明它来自第几段：`segment` 是文档里【第 N 段】的编号（从 0 开始，
  是**全文**的编号，不是本次这段的）。
  写不出这一条段号的结论不要写——没有出处的结论没法核验。
- 文档里没有的东西不要写。没有实体就写空的 `entity_pages` 数组。
- 本次读到的是全文的第 {start} 到第 {end} 段。
- 文档：{document}

JSON：
"""

#: The model is asked whether the new conclusions contradict the old
#: ones. A conclusion the document itself contradicts is not evidence
#: for anything - the reader has to see both versions.
CONFLICT_PROMPT = """\
下面是一份文档**重新编译**后得到的新结论，以及同一页面上原有的旧结论。

请判断：有没有哪一条新结论跟哪一条旧结论**矛盾**——同一件事，两种说法不能同时成立
（比如旧版写「验收期限为 30 日」、新版写「验收期限为 15 日」）。

只输出 JSON：
{{
  "contradictions": [
    {{"old_index": 0, "new_index": 1, "topic": "一句话概括这件事", "reason": "为什么这两条不能同时成立"}}
  ]
}}

`old_index` / `new_index` 是下面两栏里的编号（从 0 开始）。
没有矛盾就输出空的 `contradictions` 数组。
**只是措辞不同、意思一样的不算矛盾**；新旧一致也不算。

旧结论：
{old}

新结论：
{new}

JSON：
"""


class CoreCompiler:
    """Reads a core document and writes down what it says."""

    def __init__(
        self,
        sqlite_store: Any,
        llm_gateway: Any | None = None,
        timeout: float | None = None,
        segment_chars: int = DEFAULT_SEGMENT_CHARS,
        read_chars: int | None = None,
    ):
        self.sqlite = sqlite_store
        self.llm = llm_gateway
        self.timeout = timeout if timeout is not None else DEFAULT_COMPILE_TIMEOUT
        self.segment_chars = segment_chars
        # A document that does not fit into one read is read in parts.
        self.read_chars = read_chars if read_chars is not None else default_read_chars()

    async def compile(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Compile a core document into pages.

        A failure is reported, not raised: the file is marked as "to be
        compiled again" and the rest of the build goes on.
        """
        doc_id = str(doc.get("doc_id") or "")
        text = str(doc.get("text") or "")
        segments = self._segments(doc, text)
        if not doc_id or not text.strip():
            return {"compiled": False, "error": "文档没有内容", "pages": []}

        try:
            result = await asyncio.wait_for(
                self._compile(doc, segments), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            logger.warning(f"The compilation of {doc_id} ran out of time")
            await self._mark(doc_id, "pending_recompile")
            return {"compiled": False, "error": "编译超时", "pages": []}
        except Exception as e:  # noqa: BLE001 - a failed compile is not a failed build
            logger.warning(f"The compilation of {doc_id} failed: {e}")
            await self._mark(doc_id, "pending_recompile")
            return {"compiled": False, "error": str(e), "pages": []}

        if not result["pages"]:
            await self._mark(doc_id, "pending_recompile")
            return {"compiled": False, "error": "没有页面产出", "pages": []}

        await self._mark(doc_id, "compiled", segments=len(segments))
        return result

    async def _compile(
        self, doc: dict[str, Any], segments: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """The whole compilation: the model reads the document, and what
        it says is written down."""
        # A document that does not fit into one read is read in parts
        # (AC-E-013); a document that fits is read in one go.
        pages = self._merge_pages(
            [
                page
                for window in self._windows(segments)
                for page in await self._compile_pages(doc, window)
            ]
        )

        written: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        for page in pages:
            try:
                page_conflicts = await self._write_page(doc, page, segments)
                written.append(page)
                conflicts.extend(page_conflicts)
            except Exception as e:  # noqa: BLE001 - one page is not the whole compile
                logger.warning(
                    f"A page of {doc.get('doc_id') or ''} could not be written: {e}"
                )

        return {"compiled": True, "pages": written, "conflicts": conflicts}

    # --------------------------------------------------------------------------
    # the model
    # --------------------------------------------------------------------------

    async def _compile_pages(
        self, doc: dict[str, Any], window: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Ask the model what this part of the document says."""
        if self.llm is None:
            raise RuntimeError("没有模型，无法编译核心层文档")

        from yuanwork.llm.gateway import LLMMessage, LLMTask

        prompt = COMPILE_PROMPT.format(
            document=self._document_text(window),
            start=window[0]["index"],
            end=window[-1]["index"],
        )
        task = LLMTask(
            task_type="wiki_compile",
            messages=[LLMMessage(role="user", content=prompt)],
            temperature=0.0,
            max_tokens=4000,
            json_mode=True,
        )
        response = await self.llm.complete(task)
        return self._read_pages(response.content)

    def _read_pages(self, content: str) -> list[dict[str, Any]]:
        """The pages in the model's answer."""
        data = self._read_json(content)
        if not isinstance(data, dict):
            return []

        pages: list[dict[str, Any]] = []
        for section, page_type in (
            ("entity_pages", PAGE_TYPE_ENTITY),
            ("concept_pages", PAGE_TYPE_CONCEPT),
            ("comparison_tables", PAGE_TYPE_COMPARISON),
        ):
            for page in data.get(section) or []:
                if not isinstance(page, dict):
                    continue
                title = str(page.get("title") or "").strip()
                if not title:
                    continue
                pages.append(
                    {
                        "title": title,
                        "summary": str(page.get("summary") or "").strip()[:300],
                        "key_points": [
                            str(point).strip()
                            for point in (page.get("key_points") or [])
                            if str(point).strip()
                        ][:10],
                        "conclusions": [
                            {
                                "text": str(conclusion.get("text") or "").strip(),
                                "segment": int(conclusion.get("segment") or 0),
                            }
                            for conclusion in (page.get("conclusions") or [])
                            if isinstance(conclusion, dict)
                            and str(conclusion.get("text") or "").strip()
                        ],
                        "page_type": page_type,
                    }
                )
        return pages

    async def _find_conflicts(
        self,
        old: list[dict[str, Any]],
        new: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        """Where the new conclusions contradict the old ones.

        The second value says whether the check ran at all: a check that
        failed is not a check that found nothing, and the two are kept
        apart so that a failure never silently drops the old version.
        """
        if not old or not new or self.llm is None:
            return [], True

        from yuanwork.llm.gateway import LLMMessage, LLMTask

        prompt = CONFLICT_PROMPT.format(
            old=self._conclusion_list(old), new=self._conclusion_list(new)
        )
        task = LLMTask(
            task_type="wiki_compile",
            messages=[LLMMessage(role="user", content=prompt)],
            temperature=0.0,
            max_tokens=1500,
            json_mode=True,
        )
        try:
            response = await self.llm.complete(task)
        except Exception as e:  # noqa: BLE001 - the old version is kept anyway
            logger.warning(f"The contradiction check failed: {e}")
            return [], False

        try:
            return self._read_conflicts(response.content, old, new), True
        except Exception as e:  # noqa: BLE001 - the old version is kept anyway
            logger.warning(f"The contradiction check could not be read: {e}")
            return [], False

    def _read_conflicts(
        self,
        content: str,
        old: list[dict[str, Any]],
        new: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """The contradictions the model found."""
        data = self._read_json(content)
        if not isinstance(data, dict):
            return []

        conflicts: list[dict[str, Any]] = []
        for item in data.get("contradictions") or []:
            if not isinstance(item, dict):
                continue
            old_index = int(item.get("old_index", -1))
            new_index = int(item.get("new_index", -1))
            if not (0 <= old_index < len(old) and 0 <= new_index < len(new)):
                continue
            conflicts.append(
                {
                    "old": old[old_index],
                    "new": new[new_index],
                    "topic": str(item.get("topic") or "").strip(),
                    "reason": str(item.get("reason") or "").strip(),
                }
            )
        return conflicts

    def _read_json(self, content: str) -> Any:
        """The JSON in the model's answer."""
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)

    # --------------------------------------------------------------------------
    # the document
    # --------------------------------------------------------------------------

    def _segments(self, doc: dict[str, Any], text: str) -> list[dict[str, Any]]:
        """The pieces the model reads, one by one.

        A piece that came from the parser keeps the anchor of the block
        it was cut from - that is what lets a conclusion say where in the
        original it came from. A document that arrives without pieces
        (or one cut out of plain text) has anchors nowhere: its
        conclusions cannot be checked, and so they are not written down
        as conclusions.
        """
        raw = doc.get("segments")
        segments: list[dict[str, Any]] = []

        if raw:
            for item in raw:
                body = str(item.get("text") or "")
                if not body.strip():
                    continue
                anchor = item.get("anchor")
                anchor = anchor if isinstance(anchor, dict) and anchor else None
                for piece in self._cut(body):
                    segments.append({"text": piece, "anchor": anchor})
        else:
            for paragraph in text.split("\n\n"):
                if not paragraph.strip():
                    continue
                for piece in self._cut(paragraph):
                    segments.append({"text": piece, "anchor": None})

        # Every piece knows where it is in the whole document - that is
        # what the conclusion says when it is written down.
        for index, segment in enumerate(segments):
            segment["index"] = index
        return segments

    def _cut(self, text: str) -> list[str]:
        """Cut a piece that is too long for one read."""
        if len(text) <= self.segment_chars:
            return [text]

        pieces: list[str] = []
        current = ""
        for paragraph in text.split("\n\n"):
            if current and len(current) + len(paragraph) > self.segment_chars:
                pieces.append(current)
                current = paragraph
            else:
                current = f"{current}\n\n{paragraph}" if current else paragraph
        if current:
            pieces.append(current)
        return pieces

    def _document_text(self, segments: list[dict[str, Any]]) -> str:
        """The document as the model reads it.

        The pieces are numbered - the conclusions say which piece they
        came from, and that is what the anchor is looked up by. The
        numbers are the ones of the whole document, so a conclusion
        read in the third part still says which part it came from.
        """
        return "\n\n".join(
            f"【第 {segment.get('index', 0)} 段】\n{segment['text']}"
            for segment in segments
        )

    def _windows(
        self, segments: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        """The document, cut into the parts one read can hold.

        A document that fits into one read is one window. A longer one
        is read in several goes, and every conclusion says which part
        it was read from (AC-E-013).
        """
        if not segments:
            return []

        windows: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        length = 0
        for segment in segments:
            size = len(segment["text"])
            if current and length + size > self.read_chars:
                windows.append(current)
                current = []
                length = 0
            current.append(segment)
            length += size
        if current:
            windows.append(current)
        return windows

    def _merge_pages(self, pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The pages of several reads, put together.

        The same page can come out of more than one read - the parties
        of a contract are mentioned in the first part and again in the
        last. Their conclusions are put together, and the same point is
        not written twice.
        """
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        order: list[tuple[str, str]] = []
        for page in pages:
            key = (page["page_type"], page["title"])
            if key not in merged:
                merged[key] = {**page, "conclusions": [], "key_points": []}
                order.append(key)
            target = merged[key]
            target["conclusions"].extend(page.get("conclusions") or [])
            for point in page.get("key_points") or []:
                if point not in target["key_points"]:
                    target["key_points"].append(point)
        return [merged[key] for key in order]

    def _anchor_of(
        self, segments: list[dict[str, Any]], conclusion: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Where in the original this conclusion came from.

        The text of the conclusion is looked for in the document first:
        a conclusion that is written down word for word is where it
        says it is. When the model summarised instead of quoting, the
        piece it pointed at is used.
        """
        text = str(conclusion.get("text") or "").strip()
        if text:
            for segment in segments:
                if text in str(segment.get("text") or ""):
                    anchor = segment.get("anchor")
                    if anchor:
                        return anchor

        index = int(conclusion.get("segment") or 0)
        if 0 <= index < len(segments):
            anchor = segments[index].get("anchor")
            if anchor:
                return anchor
        return None

    # --------------------------------------------------------------------------
    # writing it down
    # --------------------------------------------------------------------------

    async def _write_page(
        self,
        doc: dict[str, Any],
        page: dict[str, Any],
        segments: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Write one compiled page, and the conclusions on it.

        The contradictions with what the page said before are found
        first: both versions are kept, and the difference is put side
        by side in a comparison table (ADR-033).
        """
        from yuanwork.wiki.page_generator import (
            merge_blocks,
            render_blocks,
            slugify,
            split_paragraphs,
        )

        doc_id = str(doc.get("doc_id") or "")
        title = page["title"]
        page_id = f"core/{slugify(title)}"

        old = await self.sqlite.list_page_conclusions(page_id, active_only=False)
        conflicts, checked = await self._find_conflicts(
            old, page.get("conclusions") or []
        )

        markdown = self._render(doc, page, segment_count=len(segments))

        # The page is made of paragraphs, so the user can edit one of
        # them later without the recompile taking it away (ADR-037).
        existing = await self.sqlite.list_page_blocks(page_id)
        blocks = merge_blocks(existing, split_paragraphs(markdown))

        # The body is the merged blocks - the user's own paragraphs stay
        # in the page, not only in the table. A body that dropped them
        # would show the reader the model's text again, and the next
        # edit (which starts from the body) would take the user's words
        # away for good (AC-F-027). The manual compile path does the
        # same.
        markdown = render_blocks(blocks)

        # Every conclusion that can be pinned to a place in the original
        # is written down with that place (ADR-032). One that cannot is
        # not written down at all: a claim nobody can check cannot be
        # used as evidence either.
        written, conflicts = await self._write_conclusions(
            doc, page, page_id, segments, old, conflicts, checked
        )
        await self._link_blocks(blocks, written)

        await self.sqlite.replace_page_blocks(page_id, blocks)

        await self.sqlite.upsert_wiki_page(
            page_id=page_id,
            title=title,
            markdown=markdown,
            summary=page.get("summary", ""),
            key_points=page.get("key_points", []),
            source_chunk_ids=[],
            source_doc_ids=[doc_id] if doc_id else [],
            category=page.get("page_type", PAGE_TYPE_ENTITY),
            tags=page.get("key_points", [])[:5],
            page_type=page.get("page_type", PAGE_TYPE_ENTITY),
            tier=CORE_TIER,
            user_edited=any(b.get("owner") == "user" for b in blocks),
        )

        for conflict in conflicts:
            await self._write_comparison(conflict)

        return conflicts

    async def _write_conclusions(
        self,
        doc: dict[str, Any],
        page: dict[str, Any],
        page_id: str,
        segments: list[dict[str, Any]],
        old: list[dict[str, Any]],
        conflicts: list[dict[str, Any]],
        checked: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """The conclusions of a page, each with where it came from.

        The contradictions come back too, with the new side pointing at
        the conclusion that was actually written down.
        """
        doc_id = str(doc.get("doc_id") or "")
        source_hash = str(doc.get("source_hash") or "")

        written: list[dict[str, Any]] = []
        for conclusion in page.get("conclusions") or []:
            text = str(conclusion.get("text") or "").strip()
            if not text:
                continue
            anchor = self._anchor_of(segments, conclusion)
            if not anchor:
                # No place to point at: this one is not written down.
                continue

            conclusion_id = f"{page_id}#{_short_hash(text)}"
            await self.sqlite.upsert_page_conclusion(
                conclusion_id=conclusion_id,
                page_id=page_id,
                text=text,
                anchor=anchor,
                source_doc_id=doc_id,
                source_hash=source_hash,
                segment_index=int(conclusion.get("segment") or 0),
            )
            written.append(
                {
                    "id": conclusion_id,
                    "text": text,
                    "anchor": anchor,
                    "source_doc_id": doc_id,
                    "segment_index": int(conclusion.get("segment") or 0),
                }
            )

        # The conclusions that are contradicted are kept - both versions
        # are there for the reader to compare (ADR-033).
        resolved: list[dict[str, Any]] = []
        for conflict in conflicts:
            new_row = next(
                (
                    conclusion
                    for conclusion in written
                    if conclusion["text"] == conflict["new"]["text"]
                ),
                None,
            )
            if new_row is None:
                # The new conclusion has no place to point at, so it was
                # not written down - there is nothing to compare.
                continue
            conflict = {**conflict, "new": new_row}
            topic = conflict["topic"] or conflict["old"]["text"]
            group = f"conflict-{page_id}-{_short_hash(topic)}"
            await self.sqlite.mark_page_conflict(
                conflict_group=group,
                new_id=new_row["id"],
                old_ids=[conflict["old"]["id"]],
            )
            conflict["group"] = group
            resolved.append(conflict)

        # The conclusions the new compilation does not have are gone -
        # they are not evidence any more. The ones that contradict
        # something stay, and so do the ones of a check that failed:
        # nothing is dropped quietly.
        keep = [conclusion["id"] for conclusion in written]
        keep += [conflict["old"]["id"] for conflict in resolved]
        if not checked:
            keep += [conclusion["id"] for conclusion in old]
        await self.sqlite.delete_page_conclusions_except(page_id, keep)
        return written, resolved

    async def _write_comparison(self, conflict: dict[str, Any]) -> None:
        """The table that puts the two versions side by side."""
        from yuanwork.wiki.page_generator import (
            merge_blocks,
            render_blocks,
            slugify,
            split_paragraphs,
        )

        old = conflict["old"]
        new = conflict["new"]
        topic = conflict.get("topic") or new["text"][:20]
        page_id = f"core/对比-{slugify(topic)}"

        old_source = await self._source_of(old)
        new_source = await self._source_of(new)

        parts = [
            f"# 对比：{topic}",
            "",
            conflict.get("reason") or "新旧两版说法不一致。",
            "",
            "| 版本 | 说法 | 出处 |",
            "|------|------|------|",
            f"| 旧版 | {old['text']} | {old_source} |",
            f"| 新版 | {new['text']} | {new_source} |",
            "",
            "## 结论",
            "",
            f"- 旧版：{old['text']}",
            f"- 新版：{new['text']}",
            "",
        ]
        markdown = "\n".join(parts)

        existing = await self.sqlite.list_page_blocks(page_id)
        blocks = merge_blocks(existing, split_paragraphs(markdown))

        # The body is the merged blocks - the user's own paragraphs stay
        # in the page, not only in the table (AC-F-027). The same the
        # other pages do.
        markdown = render_blocks(blocks)

        await self.sqlite.replace_page_blocks(page_id, blocks)

        await self.sqlite.upsert_wiki_page(
            page_id=page_id,
            title=f"对比：{topic}",
            markdown=markdown,
            summary=conflict.get("reason") or f"旧版与新版对「{topic}」的说法不一致。",
            key_points=[f"旧版：{old['text']}", f"新版：{new['text']}"],
            source_chunk_ids=[],
            source_doc_ids=[
                doc_id
                for doc_id in (old.get("source_doc_id"), new.get("source_doc_id"))
                if doc_id
            ],
            category=PAGE_TYPE_COMPARISON,
            tags=["矛盾", "双版本"],
            page_type=PAGE_TYPE_COMPARISON,
            tier=CORE_TIER,
            user_edited=any(b.get("owner") == "user" for b in blocks),
        )

    async def _source_of(self, conclusion: dict[str, Any]) -> str:
        """Where a version of a conclusion was read - in words."""
        from yuanwork.utils.anchor import anchor_location

        file_row = await self.sqlite.get_file(conclusion.get("source_doc_id") or "")
        name = file_row["file_name"] if file_row else ""
        location = anchor_location(conclusion.get("anchor") or {})
        if name and location:
            return f"《{name}》{location}"
        if name:
            return f"《{name}》"
        return location

    async def _link_blocks(
        self, blocks: list[dict[str, Any]], conclusions: list[dict[str, Any]]
    ) -> None:
        """Say which block is which conclusion.

        The block that holds the conclusion is the one that carries the
        link - so a reader can go from the paragraph to the place it was
        read from.
        """
        for block in blocks:
            text = block.get("text", "")
            for conclusion in conclusions:
                if conclusion["text"] and conclusion["text"] in text:
                    block["conclusion_id"] = conclusion["id"]
                    break

    def _conclusion_list(self, conclusions: list[dict[str, Any]]) -> str:
        """The conclusions as the model reads them."""
        return "\n".join(
            f"[{index}] {conclusion.get('text', '')}"
            for index, conclusion in enumerate(conclusions)
        )

    def _render(
        self, doc: dict[str, Any], page: dict[str, Any], segment_count: int = 1
    ) -> str:
        """The page as markdown.

        A document that was read in parts says which part each
        conclusion came from - the reader has to know whether a
        conclusion is the only thing the document says about a subject,
        or one of several (AC-E-013).
        """
        parts = [f"# {page['title']}", ""]
        if page.get("summary"):
            parts += [page["summary"], ""]
        key_points = page.get("key_points") or []
        if key_points:
            parts.append("## 要点")
            for point in key_points:
                parts.append(f"- {point}")
            parts.append("")
        conclusions = page.get("conclusions") or []
        if conclusions:
            parts.append("## 结论")
            for conclusion in conclusions:
                text = conclusion["text"]
                if segment_count > 1:
                    text = f"{text}（第 {conclusion.get('segment', 0)} 段）"
                parts.append(f"- {text}")
            parts.append("")
        source = str(doc.get("file_name") or "").strip()
        if source:
            parts += ["## 出处", f"[1] 《{source}》", ""]
        return "\n".join(parts)

    async def _mark(
        self, doc_id: str, status: str, segments: int = 0
    ) -> None:
        """How far the compilation got."""
        try:
            existing = await self.sqlite.get_doc_tier(doc_id)
            if existing is None:
                # The document was never placed in a layer - it is a
                # core document all the same, it is being compiled.
                await self.sqlite.upsert_doc_tier(
                    doc_id=doc_id,
                    tier=CORE_TIER,
                    decided_by="rule",
                    compile_status=status,
                    segment_count=segments,
                )
                return
            await self.sqlite.set_doc_compile_status(doc_id, status)
            if segments:
                await self.sqlite.set_doc_segment_count(doc_id, segments)
        except Exception as e:  # noqa: BLE001 - the record is a nicety
            logger.debug(f"The compile status of {doc_id} was not written: {e}")
