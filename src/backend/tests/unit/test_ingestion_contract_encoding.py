"""TASK-009 — the text parser's encoding fallback.

* AC-E-003 — a file whose bytes are not valid UTF-8 is decoded as GBK
  with the invalid bytes dropped, and the resulting garbage is indexed
  as if it were text
"""

from __future__ import annotations

from pathlib import Path

from yuanwork.ingestion.parser.md_txt_parser import MarkdownTxtParser
from unit.ingestion_fixtures import make_binary_file, make_text_file


async def test_binary_content_is_decoded_as_gbk_and_indexed(temp_dir: Path):
    # \xff is never a valid UTF-8 lead byte, so the UTF-8 read fails
    path = make_binary_file(temp_dir, "binary.txt")

    blocks = await MarkdownTxtParser().parse(path, "doc1")

    assert len(blocks) == 1
    block = blocks[0]
    assert block.type == "text"
    # the leading control bytes survive; the invalid ones were dropped
    assert block.content.startswith("\x00\x01\x02")
    assert len(block.content) > 0


async def test_the_garbage_is_not_silently_dropped(temp_dir: Path):
    """The whole point of this contract: binary content is *not*
    rejected, it ends up in the knowledge base."""
    path = make_binary_file(temp_dir, "binary.txt")
    blocks = await MarkdownTxtParser().parse(path, "doc1")
    assert blocks, "binary content was dropped instead of being indexed"


async def test_utf8_content_is_read_as_utf8(temp_dir: Path):
    path = make_text_file(temp_dir, "ok.txt", "正常的中文内容")
    blocks = await MarkdownTxtParser().parse(path, "doc1")
    assert len(blocks) == 1
    assert blocks[0].content == "正常的中文内容"


async def test_gbk_content_survives_the_fallback(temp_dir: Path):
    """Content that is valid GBK but not valid UTF-8 keeps its characters."""
    path = temp_dir / "gbk.txt"
    path.write_bytes("中文内容".encode("gbk"))
    blocks = await MarkdownTxtParser().parse(path, "doc1")
    assert len(blocks) == 1
    assert blocks[0].content == "中文内容"
