"""Excel (.xlsx) parser using openpyxl."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from yuanwork.ingestion.parser.base import BaseParser, RawBlock

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

                # Convert sheet to rows
                rows = []
                for row in ws.iter_rows(values_only=True):
                    if any(cell is not None and str(cell).strip() for cell in row):
                        rows.append([
                            str(cell).strip() if cell is not None else ""
                            for cell in row
                        ])

                if not rows:
                    continue

                # Convert to Markdown table
                table_md = self._rows_to_markdown(rows)

                blocks.append(
                    RawBlock(
                        block_id=f"{doc_id}_sheet{sheet_idx}",
                        doc_id=doc_id,
                        content=table_md,
                        type="table",
                        meta={
                            "sheet_name": sheet_name,
                            "sheet_index": sheet_idx,
                            "sheet_total": total_sheets,
                            "row_count": len(rows),
                        },
                    )
                )
        finally:
            wb.close()

        return blocks

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
