"""Tests for the XLSX parser (openpyxl-based)."""

from __future__ import annotations

from pathlib import Path

import pytest

from yuanwork.ingestion.parser.xlsx_parser import XlsxParser


def _make_xlsx(path: Path) -> None:
    """Create a small two-sheet xlsx for testing."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "项目进度"
    ws.append(["任务", "负责人", "状态", "截止日期"])
    ws.append(["需求评审", "张三", "已完成", "2024-12-01"])
    ws.append(["架构设计", "李四", "进行中", "2025-01-15"])

    ws2 = wb.create_sheet("预算")
    ws2.append(["项目", "金额(万元)", "备注"])
    ws2.append(["人力成本", 120, "开发 4 人 3 月"])
    ws2.append(["云服务", 25, "阿里云 ECS + RDS"])

    # A third empty sheet to verify we skip blank sheets
    ws3 = wb.create_sheet("空表")
    wb.save(path)


@pytest.mark.asyncio
async def test_xlsx_parser_produces_row_blocks(temp_dir: Path):
    """One block per row: a chunk that covers a whole sheet cannot say
    which row it came from."""
    xlsx = temp_dir / "demo.xlsx"
    _make_xlsx(xlsx)

    parser = XlsxParser()
    blocks = await parser.parse(xlsx, "doc-xlsx-1")

    # Two non-empty sheets, three rows each
    assert len(blocks) == 6
    for b in blocks:
        assert b.type == "table"
        assert b.doc_id == "doc-xlsx-1"
        assert "sheet_name" in b.meta
        assert "sheet_index" in b.meta
        assert "sheet_total" in b.meta
        assert b.meta["anchor"]["kind"] == "sheet_row"

    # The first sheet is 项目进度, and every row knows its number
    first = blocks[0]
    assert first.meta["sheet_name"] == "项目进度"
    assert first.meta["row_index"] == 1
    assert [b.meta["row_index"] for b in blocks[:3]] == [1, 2, 3]

    # A data row carries its header, so it can be read on its own
    data_row = blocks[1]
    assert "任务" in data_row.content  # header preserved
    assert "张三" in data_row.content
    # Markdown table syntax
    assert first.content.startswith("| ")
    assert "---" in first.content

    # The last three blocks are the 预算 sheet
    second = blocks[3]
    assert second.meta["sheet_name"] == "预算"
    # its first block is the header row, the second one the data
    data = blocks[4]
    assert "人力成本" in data.content
    assert "120" in data.content
    assert data.meta["row_index"] == 2


@pytest.mark.asyncio
async def test_xlsx_parser_extract_metadata(temp_dir: Path):
    xlsx = temp_dir / "demo.xlsx"
    _make_xlsx(xlsx)

    parser = XlsxParser()
    meta = await parser.extract_metadata(xlsx)
    assert meta["sheet_count"] == 3  # two populated + one empty
    assert "项目进度" in meta["sheet_names"]
    assert "预算" in meta["sheet_names"]


@pytest.mark.asyncio
async def test_xlsx_parser_handles_empty_sheets(temp_dir: Path):
    """Sheets with no rows should be skipped, not error."""
    from openpyxl import Workbook

    xlsx = temp_dir / "all_empty.xlsx"
    wb = Workbook()
    wb.active.append([])  # one empty row
    wb.save(xlsx)

    parser = XlsxParser()
    blocks = await parser.parse(xlsx, "doc-empty")
    assert blocks == []
