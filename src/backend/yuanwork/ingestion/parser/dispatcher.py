"""Parser dispatcher - routes files to appropriate parsers."""

from __future__ import annotations

from pathlib import Path

from yuanwork.ingestion.parser.base import BaseParser

#: The old binary formats - Word 97, PowerPoint 97 and Excel 97.
#:
#: They are the *old* versions of formats this product does support, and
#: reading them means converting a compound binary document, which this
#: release does not do (proposal.md §2.2).
#:
#: They are named here so the dispatcher stops claiming to read them.
#: Every claim is a promise: as long as the dispatcher said ".doc is
#: supported", every build went and tried - and failed, every time.
LEGACY_BINARY_EXTENSIONS = (".doc", ".ppt", ".xls")

#: What the user should save each legacy file as.
LEGACY_BINARY_REPLACEMENTS = {
    ".doc": ".docx",
    ".ppt": ".pptx",
    ".xls": ".xlsx",
}

#: The phrase every "a format nobody supports" reason begins with.
#:
#: It is the agreed signal between the backend and the sidebar: the
#: reason of an unsupported file starts with it, and the reason of a
#: file that could not be read never does. The backend decides which
#: of the two lists a file belongs to; the marker is how it says so
#: (AC-E-004).
UNSUPPORTED_MARKER = "不支持的文件类型"


def unsupported_reason(file_path: Path) -> str:
    """Why this file is not indexed - in words.

    A file whose format nobody supports is not a failure and not a
    mystery: it is a format this release does not read, and the reason
    says which one (AC-E-004).

    The old binary formats get a reason of their own. "Unsupported" on
    its own sounds like the app has never heard of the format; for
    these it is the other way round - the app knows them very well and
    this release does not convert them.
    """
    suffix = file_path.suffix
    shown = suffix or "（无扩展名）"
    key = suffix.lower()
    if key in LEGACY_BINARY_EXTENSIONS:
        newer = LEGACY_BINARY_REPLACEMENTS.get(key)
        advice = f"——请另存为 {newer} 后再试" if newer else ""
        return (
            f"{UNSUPPORTED_MARKER}：{shown}"
            f"（旧版二进制格式，本期不做转换支持{advice}）"
        )
    return f"{UNSUPPORTED_MARKER}：{shown}"


class ParserDispatcher:
    """Routes files to the appropriate parser based on extension."""

    def __init__(self, parsers: list[BaseParser] | None = None):
        if parsers is None:
            from yuanwork.ingestion.parser.pdf_parser import PDFParser
            from yuanwork.ingestion.parser.docx_parser import DocxParser
            from yuanwork.ingestion.parser.pptx_parser import PptxParser
            from yuanwork.ingestion.parser.xlsx_parser import XlsxParser
            from yuanwork.ingestion.parser.md_txt_parser import MarkdownTxtParser
            from yuanwork.ingestion.parser.image_parser import ImageParser

            parsers = [
                PDFParser(),
                DocxParser(),
                PptxParser(),
                XlsxParser(),
                MarkdownTxtParser(),
                ImageParser(),
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
