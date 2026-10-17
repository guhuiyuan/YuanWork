"""File watcher - monitors directories for changes (stage 2).

Uses watchdog to listen for file system events and triggers
re-indexing when files change.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

if TYPE_CHECKING:
    from yuanwork.ingestion.pipeline import IngestionPipeline


logger = logging.getLogger(__name__)


class _YuanWorkHandler(FileSystemEventHandler):
    """Internal watchdog handler."""

    def __init__(self, on_change: Callable[[Path, str], None]):
        self.on_change = on_change

    def on_created(self, event):
        if not event.is_directory:
            self.on_change(Path(event.src_path), "created")

    def on_modified(self, event):
        if not event.is_directory:
            self.on_change(Path(event.src_path), "modified")

    def on_deleted(self, event):
        if not event.is_directory:
            self.on_change(Path(event.src_path), "deleted")

    def on_moved(self, event):
        if not event.is_directory:
            self.on_change(Path(event.dest_path), "moved")


class FileWatcher:
    """Watch a directory tree for file changes."""

    def __init__(self, pipeline: "IngestionPipeline", debounce_seconds: float = 5.0):
        self.pipeline = pipeline
        self.debounce_seconds = debounce_seconds
        self._observer: Observer | None = None
        self._pending: dict[str, asyncio.Task] = {}

    def start(self, path: Path) -> None:
        """Start watching a path."""
        path = Path(path)
        if not path.exists():
            raise ValueError(f"Path does not exist: {path}")

        self._observer = Observer()
        handler = _YuanWorkHandler(self._on_change)
        self._observer.schedule(handler, str(path), recursive=True)
        self._observer.start()
        logger.info(f"Started watching {path}")

    def stop(self) -> None:
        """Stop watching."""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
            logger.info("Stopped watching")

    def _on_change(self, file_path: Path, event_type: str) -> None:
        """Handle a file change event with debouncing."""
        key = str(file_path)

        # Cancel pending task
        if key in self._pending:
            self._pending[key].cancel()

        # Schedule new task
        async def delayed():
            await asyncio.sleep(self.debounce_seconds)
            try:
                if file_path.exists():
                    await self.pipeline.ingest_file(file_path)
                else:
                    logger.info(f"File deleted: {file_path.name}")
            except Exception as e:
                logger.exception(f"Re-index failed for {file_path}: {e}")
            finally:
                self._pending.pop(key, None)

        # This is a sync callback, so we just log for now
        # (proper async requires running in event loop)
        logger.debug(f"Detected {event_type}: {file_path.name}")
