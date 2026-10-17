"""Parser dispatcher - routes files to appropriate parsers."""

from __future__ import annotations

from pathlib import Path

from yuanwork.ingestion.parser.base import BaseParser


class ParserDispatcher:
    """Routes files to the appropriate parser based on extension."""

    def __init__(self, parsers: list[BaseParser] | None = None):
        if parsers is None:
            from yuanwork.ingestion.parser.pdf_parser import PDFParser
            from yuanwork.ingestion.parser.docx_parser import DocxParser
            from yuanwork.ingestion.parser.pptx_parser import PptxParser
            from yuanwork.ingestion.parser.xlsx_parser import XlsxParser
            from yuanwork.ingestion.parser.md_txt_parser import MarkdownTxtParser

            parsers = [
                PDFParser(),
                DocxParser(),
                PptxParser(),
                XlsxParser(),
                MarkdownTxtParser(),
            ]
        self.parsers = parsers

    def get_parser(self, file_path: Path) -> BaseParser | None:
        """Get the parser for a file based on extension."""
        for parser in self.parsers:
            if parser.can_parse(file_path):
                return parser
        return None

    def supported_extensions(self) -> set[str]:
        """Get all supported file extensions."""
        exts = set()
        for parser in self.parsers:
            exts.update(parser.SUPPORTED_EXTENSIONS)
        return exts
