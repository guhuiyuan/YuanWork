"""Ingestion pipeline - orchestrates parse → chunk → embed → index."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from yuanwork.ingestion.chunker.structural_chunker import StructuralChunker
from yuanwork.ingestion.parser.dispatcher import ParserDispatcher

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.memory.semantic import SemanticMemory


logger = logging.getLogger(__name__)


class IngestionPipeline:
    """Orchestrates the full document ingestion workflow."""

    def __init__(
        self,
        sqlite_store: "SQLiteStore",
        semantic_memory: "SemanticMemory",
    ):
        self.sqlite = sqlite_store
        self.semantic = semantic_memory
        self.dispatcher = ParserDispatcher()
        self.chunker = StructuralChunker()

    @classmethod
    def from_app(cls, app) -> "IngestionPipeline":
        """Create pipeline from FastAPI app state."""
        from yuanwork.api.deps import get_semantic_memory, get_sqlite_store

        return cls(
            sqlite_store=get_sqlite_store(),
            semantic_memory=get_semantic_memory(),
        )

    async def ingest_file(self, file_path: Path, force: bool = False) -> dict:
        """Ingest a single file.

        Args:
            file_path: Path to file
            force: Re-ingest even if hash matches existing record

        Returns:
            Dict with status and stats
        """
        file_path = Path(file_path)
        if not file_path.exists():
            return {"status": "error", "error": "File not found", "path": str(file_path)}

        # Get parser
        parser = self.dispatcher.get_parser(file_path)
        if parser is None:
            return {
                "status": "skipped",
                "error": f"No parser for extension: {file_path.suffix}",
                "path": str(file_path),
            }

        # Compute hash
        file_hash = self._hash_file(file_path)

        # Check if already indexed (skip if hash matches and not force)
        if not force:
            existing = await self.sqlite.get_file_by_hash(file_hash)
            if existing:
                logger.info(f"Skipping {file_path.name} - already indexed")
                return {
                    "status": "skipped",
                    "reason": "already_indexed",
                    "doc_id": existing["doc_id"],
                }

        # Generate doc_id
        doc_id = hashlib.sha256(
            f"{file_path}|{file_hash}".encode()
        ).hexdigest()[:16]

        try:
            # Parse
            blocks = await parser.parse(file_path, doc_id)
            if not blocks:
                return {"status": "skipped", "reason": "no_content", "doc_id": doc_id}

            # Chunk
            chunks = self.chunker.chunk(blocks, doc_id)

            # Add metadata from parser
            meta = await parser.extract_metadata(file_path)

            # Add filename/path to chunks
            for chunk in chunks:
                chunk["file_name"] = file_path.name
                chunk["file_path"] = str(file_path)
                chunk["file_type"] = file_path.suffix.lower().lstrip(".")
                chunk["meta"].setdefault("file_meta", meta)

            # Embed and index
            await self.semantic.add_chunks(chunks)

            # Update SQLite record
            await self.sqlite.upsert_file(
                doc_id=doc_id,
                file_name=file_path.name,
                file_path=str(file_path),
                file_hash=file_hash,
                file_type=file_path.suffix.lower().lstrip("."),
                size_bytes=file_path.stat().st_size,
                chunk_count=len(chunks),
                meta=meta,
            )

            logger.info(
                f"Indexed {file_path.name}: {len(chunks)} chunks "
                f"from {len(blocks)} blocks"
            )

            return {
                "status": "indexed",
                "doc_id": doc_id,
                "blocks": len(blocks),
                "chunks": len(chunks),
            }

        except Exception as e:
            logger.exception(f"Failed to ingest {file_path}: {e}")
            return {"status": "error", "error": str(e), "path": str(file_path)}

    async def ingest_directory(
        self,
        dir_path: Path,
        recursive: bool = True,
        force: bool = False,
    ) -> dict:
        """Ingest all supported files in a directory."""
        dir_path = Path(dir_path)
        if not dir_path.exists() or not dir_path.is_dir():
            return {"status": "error", "error": "Directory not found"}

        supported = self.dispatcher.supported_extensions()
        files = []
        if recursive:
            files = [f for f in dir_path.rglob("*") if f.is_file()]
        else:
            files = [f for f in dir_path.glob("*") if f.is_file()]

        files = [f for f in files if f.suffix.lower().lstrip(".") in supported]

        results = {"indexed": 0, "skipped": 0, "errors": 0, "files": []}

        for file_path in files:
            result = await self.ingest_file(file_path, force=force)
            results["files"].append({"path": str(file_path), **result})

            if result["status"] == "indexed":
                results["indexed"] += 1
            elif result["status"] == "skipped":
                results["skipped"] += 1
            else:
                results["errors"] += 1

        return results

    def _hash_file(self, file_path: Path) -> str:
        """Compute SHA256 hash of file contents."""
        sha = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha.update(chunk)
        return sha.hexdigest()
