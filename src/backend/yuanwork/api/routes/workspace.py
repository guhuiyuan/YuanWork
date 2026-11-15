"""Workspace endpoints: the folders and single files that make up the scope.

Four endpoints, and that is the whole surface:

* add a folder
* add a single file
* list the entries
* remove an entry

Removing an entry also clears what was indexed for it — an entry the user
takes back out of the workspace must not leave its chunks behind.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from yuanwork.api.deps import (
    get_semantic_memory,
    get_sqlite_store,
    optional_episodic_recorder,
)
from yuanwork.api.schemas import (
    WorkspaceEntriesResponse,
    WorkspaceEntryInfo,
    WorkspacePathRequest,
)
from yuanwork.memory.episodic_recorder import EpisodicEvent
from yuanwork.workspace import Workspace

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_workspace() -> Workspace:
    return Workspace(get_sqlite_store())


def _to_info(entry: dict) -> WorkspaceEntryInfo:
    return WorkspaceEntryInfo(
        id=entry["id"],
        path=entry["path"],
        kind=entry["kind"],
        included=bool(entry["included"]),
        added_at=entry["added_at"],
        doc_id=entry.get("doc_id"),
    )


def _norm(path: str | Path) -> str:
    return os.path.normcase(str(path))


async def _record_file_event(kind: str, path: str) -> None:
    """Adding or taking back a file is an interaction - it is recorded.

    A failure to record it must not fail the request: the file is in
    the workspace either way.
    """
    recorder = optional_episodic_recorder()
    if recorder is None:
        return
    try:
        await recorder.record(
            EpisodicEvent(
                kind=kind,
                session_id="workspace",
                subject=path,
                detail=path,
                importance=0.6,
            )
        )
    except Exception as e:  # noqa: BLE001 - a lost event is not a broken request
        logger.warning(f"Could not record the workspace event: {e}")


async def _clear_indexed_files(entry: dict, keep_paths: set[str]) -> list[str]:
    """Delete the index entries that only this workspace entry brought in.

    A file that another entry still covers is left alone: dropping the
    folder does not remove a file the user also added by hand.
    """
    sqlite = get_sqlite_store()
    semantic = get_semantic_memory()

    if entry["kind"] == "file":
        candidates = await sqlite.get_file_by_path(entry["path"])
        rows = [candidates] if candidates else []
    else:
        rows = await sqlite.list_files_under(entry["path"])

    removed: list[str] = []
    for row in rows:
        if _norm(row["file_path"]) in keep_paths:
            continue
        await semantic.delete_by_doc_id(row["doc_id"])
        await sqlite.delete_file(row["doc_id"])
        removed.append(row["file_path"])

    return removed


@router.post("/workspace/folders", response_model=WorkspaceEntryInfo)
async def add_workspace_folder(body: WorkspacePathRequest) -> WorkspaceEntryInfo:
    """Add a folder to the workspace."""
    path = Path(body.path).expanduser()
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {body.path}")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a folder: {body.path}")

    entry = await _get_workspace().add_path(path, included=body.included)
    await _record_file_event("add_file", str(path))
    return _to_info(entry)


@router.post("/workspace/files", response_model=WorkspaceEntryInfo)
async def add_workspace_file(body: WorkspacePathRequest) -> WorkspaceEntryInfo:
    """Add a single file to the workspace."""
    path = Path(body.path).expanduser()
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {body.path}")
    if not path.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {body.path}")

    entry = await _get_workspace().add_path(path, included=body.included)
    await _record_file_event("add_file", str(path))
    return _to_info(entry)


@router.get("/workspace/entries", response_model=WorkspaceEntriesResponse)
async def list_workspace_entries() -> WorkspaceEntriesResponse:
    """The workspace entries, oldest first."""
    entries = await _get_workspace().list_entries(included_only=False)
    return WorkspaceEntriesResponse(
        entries=[_to_info(e) for e in entries],
        total=len(entries),
    )


@router.delete("/workspace/entries/{entry_id}")
async def remove_workspace_entry(entry_id: str, request: Request) -> dict:
    """Remove an entry from the workspace and clear what it brought in."""
    sqlite = get_sqlite_store()
    entry = await sqlite.get_workspace_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Entry not found: {entry_id}")

    # The paths the remaining entries still cover: their index entries stay.
    keep: set[str] = set()
    for other in await sqlite.list_workspace_entries(included_only=False):
        if other["id"] == entry["id"]:
            continue
        other_path = Path(other["path"])
        if other["kind"] == "file":
            keep.add(_norm(other_path))
        else:
            for f in other_path.rglob("*"):
                if f.is_file():
                    keep.add(_norm(f))

    removed_files = await _clear_indexed_files(entry, keep)
    await sqlite.remove_workspace_entry(entry["id"])
    await _record_file_event("remove_file", entry["path"])

    return {
        "removed": entry["id"],
        "path": entry["path"],
        "indexed_files_removed": removed_files,
    }
