"""Excel (.xlsx) parser using openpyxl."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from yuanwork.ingestion.parser.base import BaseParser, RawBlock
from yuanwork.utils.anchor import make_anchor

logger = logging.getLogger(__name__)


class XlsxParser(BaseParser):
    """Parse Excel files, sheet by sheet."""

    SUPPORTED_EXTENSIONS = ("xlsx", "xls")

    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Parse xlsx into sheet-level blocks."""
        return await asyncio.to_thread(self._parse_sync, file_path, doc_id)

    def _parse_sync(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Synchronous XLSX parsing."""
        from openpyxl import load_workbook

        wb = load_workbook(file_path, data_only=True, read_only=True)
        blocks: list[RawBlock] = []

        try:
            total_sheets = len(wb.sheetnames)

            for sheet_idx, sheet_name in enumerate(wb.sheetnames):
                ws = wb[sheet_name]

                # Convert sheet to rows, keeping the row number each one
                # has in the sheet - that is the position a citation has to
                # point at.
                rows: list[tuple[int, list[str]]] = []
                for row_idx, row in enumerate(ws.iter_rows(values_only=True), start=1):
                    if any(cell is not None and str(cell).strip() for cell in row):
                        rows.append((
                            row_idx,
                            [
                                str(cell).strip() if cell is not None else ""
                                for cell in row
                            ],
                        ))

                if not rows:
                    continue

                # One block per row: a chunk that covers a whole sheet
                # cannot say which row it came from, and a row without
                # its header is unreadable on its own. The first row is
                # treated as the header - it is indexed as a row too, so
                # nothing in the sheet is lost.
                header = rows[0][1] if len(rows) > 1 else []
                for position, (row_idx, cells) in enumerate(rows):
                    content = self._row_to_markdown(
                        header if position > 0 else [], cells
                    )
                    if not content:
                        continue
                    blocks.append(
                        RawBlock(
                            block_id=f"{doc_id}_sheet{sheet_idx}_row{row_idx}",
                            doc_id=doc_id,
                            content=content,
                            type="table",
                            meta={
                                "sheet_name": sheet_name,
                                "sheet_index": sheet_idx,
                                "sheet_total": total_sheets,
                                "row_index": row_idx,
                                "row_count": len(rows),
                                "header": " | ".join(header),
                                "anchor": make_anchor(
                                    "sheet_row", sheet=sheet_name, row=row_idx
                                ),
                            },
                        )
                    )
        finally:
            wb.close()

        return blocks

    def _row_to_markdown(self, header: list[str], cells: list[str]) -> str:
        """One spreadsheet row, with the header it belongs to."""
        if not any(cells):
            return ""
        width = max(len(header), len(cells))
        head = list(header) + [""] * (width - len(header))
        row = list(cells) + [""] * (width - len(cells))
        lines = ["| " + " | ".join(row) + " |"]
        if head:
            lines.insert(0, "| " + " | ".join(head) + " |")
            lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
        return "\n".join(lines)

    def _rows_to_markdown(self, rows: list[list[str]]) -> str:
        """Convert rows to Markdown table."""
        if not rows:
            return ""

        header = rows[0]
        body = rows[1:]

        lines = ["| " + " | ".join(header) + " |"]
        lines.append("| " + " | ".join(["---"] * len(header)) + " |")
        for row in body:
            padded = row + [""] * (len(header) - len(row))
            lines.append("| " + " | ".join(padded[: len(header)]) + " |")
        return "\n".join(lines)

    async def extract_metadata(self, file_path: Path) -> dict[str, Any]:
        """Extract XLSX metadata."""
        def _extract():
            try:
                from openpyxl import load_workbook
                wb = load_workbook(file_path, read_only=True)
                try:
                    return {
                        "sheet_count": len(wb.sheetnames),
                        "sheet_names": list(wb.sheetnames),
                    }
                finally:
                    wb.close()
            except Exception as e:
                logger.warning(f"Failed to extract XLSX metadata: {e}")
                return {}

        return await asyncio.to_thread(_extract)
