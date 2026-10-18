"""Tests for document parsers."""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.ingestion.parser.base import RawBlock
from yuanwork.ingestion.parser.dispatcher import ParserDispatcher
from yuanwork.ingestion.parser.md_txt_parser import MarkdownTxtParser


@pytest.mark.asyncio
async def test_markdown_parser_basic(temp_dir: Path):
    """Markdown parser splits by headings."""
    md_content = """\
# 项目 A 总结

这是项目 A 的概述。

## 背景
项目背景介绍。

## 目标
- 目标 1
- 目标 2

# 项目 B 总结

B 项目的内容。
"""
    md_file = temp_dir / "test.md"
    md_file.write_text(md_content, encoding="utf-8")

    parser = MarkdownTxtParser()
    blocks = await parser.parse(md_file, "doc-1")

    # Should have at least 3 sections (3 headings)
    assert len(blocks) >= 3
    # All blocks are RawBlock instances
    assert all(isinstance(b, RawBlock) for b in blocks)
    # First block should be project A
    assert "项目 A" in blocks[0].content or "概述" in blocks[0].content


@pytest.mark.asyncio
async def test_dispatcher_routing():
    """Dispatcher routes files to appropriate parsers."""
    dispatcher = ParserDispatcher()

    assert dispatcher.get_parser(Path("test.pdf")) is not None
    assert dispatcher.get_parser(Path("test.docx")) is not None
    assert dispatcher.get_parser(Path("test.pptx")) is not None
    assert dispatcher.get_parser(Path("test.xlsx")) is not None
    assert dispatcher.get_parser(Path("test.md")) is not None
    assert dispatcher.get_parser(Path("test.png")) is not None
    assert dispatcher.get_parser(Path("test.jpg")) is not None
    assert dispatcher.get_parser(Path("test.unknown")) is None

    exts = dispatcher.supported_extensions()
    assert "pdf" in exts
    assert "pptx" in exts
    assert "docx" in exts
    assert "png" in exts
    assert "jpg" in exts


@pytest.mark.asyncio
async def test_plain_text_parser(temp_dir: Path):
    """Plain text parser splits by paragraph."""
    txt_content = """\
第一段内容。
关于某个主题。

第二段内容。
更多细节。

第三段。
"""
    txt_file = temp_dir / "test.txt"
    txt_file.write_text(txt_content, encoding="utf-8")

    parser = MarkdownTxtParser()
    blocks = await parser.parse(txt_file, "doc-1")

    assert len(blocks) >= 2
    # All content is captured
    all_text = " ".join(b.content for b in blocks)
    assert "第一段" in all_text
    assert "第三段" in all_text
