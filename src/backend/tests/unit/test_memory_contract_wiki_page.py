"""The Wiki page generator.

* the shape of the compiled page
* the fallback that is used when no model is available
* the source links that are written alongside the page
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yuanwork.db.sqlite import SQLiteStore
from yuanwork.wiki.page_generator import WikiPageGenerator


class FakeLLM:
    def __init__(self, content: str = ""):
        self.content = content
        self.tasks: list[Any] = []

    async def complete(self, task):
        self.tasks.append(task)
        return type("R", (), {"content": self.content})()


@pytest.fixture
async def store(tmp_path: Path):
    s = SQLiteStore(tmp_path / "store.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
def generator(store) -> WikiPageGenerator:
    return WikiPageGenerator(sqlite_store=store, llm_gateway=None)


def chunks(count: int = 2) -> list[dict]:
    return [
        {
            "id": f"c{i}",
            "doc_id": "d1",
            "text": f"这是第 {i} 段正文内容。",
            "file_name": "合同.pdf",
            "file_path": "/kb/合同.pdf",
            "meta": {"page": 12 + i},
        }
        for i in range(count)
    ]


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------


async def test_no_chunks_is_an_error(generator):
    result = await generator.compile_page(title="标题", chunks=[])
    assert result == {"error": "no_chunks"}


async def test_the_fallback_page_has_the_four_parts(generator):
    result = await generator.compile_page(title="违约金条款", chunks=chunks())
    markdown = result["markdown"]
    assert markdown.startswith("# 违约金条款")
    for section in ("摘要", "要点", "出处", "来源片段数"):
        assert section in markdown


async def test_the_fallback_page_lists_every_chunk(generator):
    result = await generator.compile_page(title="标题", chunks=chunks(3))
    assert "合同.pdf" in result["markdown"]
    assert "第 12 页" in result["markdown"]


async def test_the_page_id_defaults_to_the_slugified_title(generator):
    result = await generator.compile_page(title="违约金条款", chunks=chunks())
    assert result["page_id"] == "违约金条款"


async def test_an_explicit_page_id_is_used(generator):
    result = await generator.compile_page(
        title="标题", chunks=chunks(), page_id="auto/主题"
    )
    assert result["page_id"] == "auto/主题"


async def test_the_summary_is_extracted_from_the_markdown(generator):
    result = await generator.compile_page(title="标题", chunks=chunks())
    assert result["summary"]
    assert len(result["summary"]) <= 300


async def test_the_tags_are_extracted(generator):
    result = await generator.compile_page(title="标题", chunks=chunks())
    assert isinstance(result.get("tags", []), list)


# --------------------------------------------------------------------------
# the model path
# --------------------------------------------------------------------------


async def test_the_model_page_is_used_when_available(store):
    llm = FakeLLM(
        "# 标题\n\n> 分类：contract\n> 标签：a, b\n\n"
        "## 摘要\n\n这是摘要。\n\n## 核心内容\n\n正文。\n"
    )
    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=llm)
    result = await generator.compile_page(title="标题", chunks=chunks())
    assert "这是摘要。" in result["markdown"]
    assert llm.tasks, "the model was not called"


async def test_the_model_uses_the_compile_task(generator):
    llm = FakeLLM("# 标题\n\n## 摘要\n\n摘要。\n")
    generator_with_model = WikiPageGenerator(
        sqlite_store=generator.sqlite, llm_gateway=llm
    )
    await generator_with_model.compile_page(title="标题", chunks=chunks())
    task = llm.tasks[0]
    assert task.task_type == "wiki_compile"
    assert task.max_tokens == 3000


async def test_a_failing_model_falls_back_to_the_template(store):
    class FailingLLM:
        async def complete(self, task):
            raise RuntimeError("模型挂了")

    generator = WikiPageGenerator(sqlite_store=store, llm_gateway=FailingLLM())
    result = await generator.compile_page(title="标题", chunks=chunks())
    assert result["markdown"].startswith("# 标题")


# --------------------------------------------------------------------------
# the source links
# --------------------------------------------------------------------------


async def test_a_source_link_is_written_per_chunk(generator, store):
    await generator.compile_page(title="标题", chunks=chunks(3))
    async with store.conn.execute(
        "SELECT COUNT(*) FROM wiki_links WHERE target_type = 'chunk'"
    ) as cur:
        rows = await cur.fetchall()
    assert rows[0][0] == 3


async def test_the_links_point_from_the_page_to_the_chunks(generator, store):
    await generator.compile_page(title="标题", chunks=chunks(2))
    async with store.conn.execute(
        "SELECT source_id, source_type, target_id, target_type, link_type "
        "FROM wiki_links WHERE target_type = 'chunk'"
    ) as cur:
        rows = await cur.fetchall()
    for source_id, source_type, target_id, target_type, link_type in rows:
        assert source_type == "page"
        assert target_type == "chunk"
        assert link_type == "derived_from"
        assert target_id.startswith("c")


async def test_the_page_also_points_at_the_document_it_came_from(generator, store):
    """The page points at the slices *and* at the document - a reader
    who wants the original file can find it from the page."""
    await generator.compile_page(title="标题", chunks=chunks(2))
    async with store.conn.execute(
        "SELECT source_id, source_type, target_id, target_type, link_type "
        "FROM wiki_links WHERE target_type = 'doc'"
    ) as cur:
        rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "标题"
    assert rows[0][2] == "d1"


async def test_the_page_is_written_to_the_store(generator, store):
    result = await generator.compile_page(title="违约金条款", chunks=chunks())
    page = await store.get_wiki_page(result["page_id"])
    assert page is not None
    assert page["title"] == "违约金条款"


async def test_recompiling_replaces_the_page_and_bumps_the_version(generator, store):
    first = await generator.compile_page(title="标题", chunks=chunks(2))
    second = await generator.compile_page(title="标题", chunks=chunks(3))
    assert first["page_id"] == second["page_id"]
    page = await store.get_wiki_page(first["page_id"])
    assert page["version"] == 2


async def test_the_old_source_links_are_not_cleaned_up(generator, store):
    """Recompiling from *different* chunks adds links; the ones pointing at
    the previous chunks stay as orphans."""
    await generator.compile_page(title="标题", chunks=chunks(2))
    other = [
        {
            "id": f"x{i}", "doc_id": "d2", "text": f"新的第 {i} 段。",
            "file_name": "合同.pdf", "file_path": "/kb/合同.pdf",
            "meta": {"page": 20 + i},
        }
        for i in range(3)
    ]
    await generator.compile_page(title="标题", chunks=other)
    async with store.conn.execute(
        "SELECT COUNT(*) FROM wiki_links WHERE target_type = 'chunk'"
    ) as cur:
        rows = await cur.fetchall()
    # 2 from the first compile + 3 from the second, nothing removed
    assert rows[0][0] == 5
