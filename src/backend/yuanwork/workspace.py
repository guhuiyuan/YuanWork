"""The workspace: the folders and single files the user picked.

The workspace is the *scope* of the knowledge base. It is deliberately
small: a folder plus whatever single files were added by hand. Nothing
here watches the file system, parses documents or decides what is
indexed — it only remembers what the user said is in scope.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


def _norm(path: str | Path) -> str:
    """A comparable form of a path (case-folded on Windows)."""
    return os.path.normcase(str(path))


class Workspace:
    """The set of folders and single files that make up the working domain."""

    def __init__(self, sqlite_store: "SQLiteStore"):
        self.sqlite = sqlite_store

    # ===== entries =====

    async def add_path(self, path: str | Path, included: bool = True) -> dict:
        """Add a folder or a single file to the workspace.

        The same path is only registered once — adding it again returns the
        entry that is already there.

        `included` says whether the entry takes part in the build. A caller
        that says "do not index this" gets an entry that says so — the flag
        is not accepted and then thrown away.

        Raises:
            FileNotFoundError: the path does not exist.
        """
        target = Path(path).expanduser()
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {target}")

        resolved = str(target.resolve())
        kind = "folder" if target.is_dir() else "file"
        return await self.sqlite.add_workspace_entry(resolved, kind, included=included)

    async def list_entries(self, included_only: bool = True) -> list[dict]:
        """The workspace entries, oldest first."""
        return await self.sqlite.list_workspace_entries(included_only=included_only)

    async def remove_path(self, path: str | Path) -> bool:
        """Remove the entry at a path. Returns whether there was one."""
        entry = await self.sqlite.get_workspace_entry_by_path(str(path))
        if entry is None:
            return False
        return await self.sqlite.remove_workspace_entry(entry["id"])

    async def set_included(self, path: str | Path, included: bool) -> bool:
        """Turn indexing on or off for the entry at a path."""
        entry = await self.sqlite.get_workspace_entry_by_path(str(path))
        if entry is None:
            return False
        return await self.sqlite.set_workspace_entry_included(entry["id"], included)

    # ===== files =====

    async def list_files(self, included_only: bool = True) -> list[Path]:
        """Every file the workspace covers.

        Folders are expanded, single files are taken as they are, and a file
        that is covered twice (a file added by hand *and* sitting in an
        added folder) appears once.

        The expansion runs in a worker thread. A workspace can hold a
        hundred thousand files, and the walk is slow synchronous disk
        I/O - doing it on the event loop would stall every other
        request for as long as it takes (ADR-052).
        """
        entries = await self.list_entries(included_only=included_only)
        return await asyncio.to_thread(self._expand_entries, entries)

    def _expand_entries(self, entries: list[dict]) -> list[Path]:
        """The files the workspace entries cover - the slow part, on a thread."""
        files: list[Path] = []
        seen: set[str] = set()

        for entry in entries:
            target = Path(entry["path"])
            if entry["kind"] == "file":
                candidates = [target]
            else:
                try:
                    candidates = sorted(f for f in target.rglob("*") if f.is_file())
                except OSError:
                    logger.warning("Cannot read workspace folder %s", target)
                    continue

            for candidate in candidates:
                key = _norm(candidate)
                if key in seen:
                    continue
                seen.add(key)
                files.append(candidate)

        return files
