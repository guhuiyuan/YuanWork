"""Base parser interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any


class RawBlock:
    """A single parsed block from a document.

    The universal intermediate representation between parsers and chunkers.
    """

    def __init__(
        self,
        block_id: str,
        doc_id: str,
        content: str,
        type: str = "text",  # 'text' | 'table' | 'image_ocr' | 'code' | 'heading'
        meta: dict[str, Any] | None = None,
    ):
        self.block_id = block_id
        self.doc_id = doc_id
        self.content = content
        self.type = type
        self.meta = meta or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_id": self.block_id,
            "doc_id": self.doc_id,
            "content": self.content,
            "type": self.type,
            "meta": self.meta,
        }


class BaseParser(ABC):
    """Abstract base class for document parsers."""

    SUPPORTED_EXTENSIONS: tuple[str, ...] = ()

    @abstractmethod
    async def parse(self, file_path: Path, doc_id: str) -> list[RawBlock]:
        """Parse a file into RawBlocks.

        Args:
            file_path: Path to the file
            doc_id: Unique document ID (for block_id generation)

        Returns:
            List of RawBlock objects representing the document's content
        """
        pass

    @abstractmethod
    async def extract_metadata(self, file_path: Path) -> dict[str, Any]:
        """Extract file-level metadata (title, author, etc.)."""
        pass

    def can_parse(self, file_path: Path) -> bool:
        """Check if this parser can handle the file."""
        return file_path.suffix.lower().lstrip(".") in self.SUPPORTED_EXTENSIONS
