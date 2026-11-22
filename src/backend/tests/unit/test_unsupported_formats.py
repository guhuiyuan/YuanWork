"""TASK-091 — the formats the specification excludes are not tried.

proposal.md §2.2 says this release does **not** do the old binary
formats: ``.doc`` / ``.ppt`` / ``.xls``. The parsers used to claim them
anyway, so every rebuild went and tried — and failed, every time. The
real workspace has 46 such files, and every one of them was reported to
the user as "a file that could not be read".

That is not what it is. It is a format nobody supports — and the
difference matters, because the two are two different problems and the
user should not have to tell them apart:

* a file that could not be read is a file this release *would* have
  read if it were not broken (AC-E-005);
* a format nobody supports is a file this release does not read at
  all, and never will (AC-E-004).

What these tests pin:

* the dispatcher does not claim the old binary formats any more, so the
  build does not go and try them;
* they land in the "format nobody supports" list with a reason that
  says which format it is and what to do about it;
* they are still **listed** — a file that is not read is not a file
  that is silently dropped (AC-E-004);
* a genuinely broken file is still "could not be read", and it does not
  stop the other files (AC-E-005).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yuanwork.api.routes.ingest import MAX_FAILED_FILES, _collect_failed_files
from yuanwork.ingestion.parser.dispatcher import (
    LEGACY_BINARY_EXTENSIONS,
    UNSUPPORTED_MARKER,
    ParserDispatcher,
    unsupported_reason,
)
from yuanwork.ingestion.parser.docx_parser import DocxParser
from yuanwork.ingestion.parser.pptx_parser import PptxParser
from yuanwork.ingestion.parser.xlsx_parser import XlsxParser
from yuanwork.ingestion.pipeline import IngestionPipeline
from unit.ingestion_fixtures import (
    RecordingSemanticMemory,
    RecordingSQLite,
    make_docx_file,
    make_markdown_file,
    write_bytes,
)

#: The extensions the specification excludes.
LEGACY = ("doc", "ppt", "xls")

#: The extensions the product really reads.
SUPPORTED = {
    "pdf",
    "docx",
    "pptx",
    "xlsx",
    "md",
    "markdown",
    "txt",
    "png",
    "jpg",
    "jpeg",
    "bmp",
    "tiff",
    "tif",
    "webp",
}


def _pipeline() -> IngestionPipeline:
    """A pipeline with no database, no vector store and no model."""
    return IngestionPipeline(
        sqlite_store=RecordingSQLite(), semantic_memory=RecordingSemanticMemory()
    )


class _FakeWorkspace:
    """A workspace that holds a fixed list of files."""

    def __init__(self, files: list[Path]):
        self._files = list(files)

    async def list_entries(self, included_only: bool = True) -> list:
        return []

    async def list_files(self, included_only: bool = True) -> list[Path]:
        return list(self._files)


# --------------------------------------------------------------------------
# the dispatcher does not claim them
# --------------------------------------------------------------------------


def test_the_old_binary_formats_are_not_among_the_supported_extensions():
    """The claim is the promise — and the promise could not be kept.

    As long as the dispatcher said ".doc is supported", every build
    went and tried, and every build failed on it (TASK-091).
    """
    extensions = ParserDispatcher().supported_extensions()

    for ext in LEGACY:
        assert ext not in extensions
    assert extensions == SUPPORTED


@pytest.mark.parametrize("ext", LEGACY)
def test_the_dispatcher_has_no_parser_for_the_old_binary_formats(ext: str):
    assert ParserDispatcher().get_parser(Path(f"sample.{ext}")) is None


@pytest.mark.parametrize("ext", LEGACY)
def test_the_new_format_still_has_its_parser(ext: str):
    """Only the *old* versions are gone — the new ones still work."""
    newer = {"doc": "docx", "ppt": "pptx", "xls": "xlsx"}[ext]
    assert ParserDispatcher().get_parser(Path(f"sample.{newer}")) is not None


@pytest.mark.parametrize(
    "parser",
    [DocxParser(), PptxParser(), XlsxParser()],
    ids=["docx", "pptx", "xlsx"],
)
def test_the_parsers_do_not_claim_the_old_binary_formats(parser):
    for ext in LEGACY:
        assert ext not in parser.SUPPORTED_EXTENSIONS


def test_the_legacy_formats_are_named_in_one_place():
    assert LEGACY_BINARY_EXTENSIONS == (".doc", ".ppt", ".xls")


# --------------------------------------------------------------------------
# the build does not try them
# --------------------------------------------------------------------------


def test_the_old_binary_formats_are_not_even_read(temp_dir: Path):
    """The build does not go and try them — that is the whole point."""
    legacy = [
        write_bytes(temp_dir / "旧文档.doc", b"\xd0\xcf\x11\xe0legacy"),
        write_bytes(temp_dir / "旧课件.ppt", b"\xd0\xcf\x11\xe0legacy"),
        write_bytes(temp_dir / "旧表格.xls", b"\xd0\xcf\x11\xe0legacy"),
    ]
    good = make_markdown_file(temp_dir, "note.md", "# 标题\n")

    ready, unsupported, oversized = _pipeline().partition_workspace_files(
        [*legacy, good]
    )

    # The good file is the only one the build touches.
    assert ready == [good]
    assert oversized == []
    assert [Path(item["path"]).name for item in unsupported] == [
        "旧文档.doc",
        "旧课件.ppt",
        "旧表格.xls",
    ]


def test_the_folder_build_does_not_walk_them_either(temp_dir: Path):
    """The plain directory build reads the same set of files."""
    write_bytes(temp_dir / "旧文档.doc", b"legacy")
    make_markdown_file(temp_dir, "note.md", "# 标题\n")

    supported = _pipeline().supported_files(temp_dir)

    assert [path.name for path in supported] == ["note.md"]


# --------------------------------------------------------------------------
# the reason is in words
# --------------------------------------------------------------------------


def test_the_reason_says_which_format_it_is_and_what_to_do():
    """「旧版二进制格式，本期不做转换支持」比笼统的「格式不支持」更准."""
    reason = unsupported_reason(Path("D:/kb/旧文档.doc"))

    assert "旧版二进制格式" in reason
    assert "本期不做转换支持" in reason
    assert ".docx" in reason          # what to save it as
    assert reason.startswith("不支持的文件类型：")


@pytest.mark.parametrize(
    "name,newer",
    [("旧文档.doc", ".docx"), ("旧课件.ppt", ".pptx"), ("旧表格.xls", ".xlsx")],
)
def test_every_legacy_format_points_at_its_new_version(name: str, newer: str):
    assert newer in unsupported_reason(Path(f"D:/kb/{name}"))


def test_a_format_nobody_knows_gets_the_plain_reason():
    """The old binary formats are not the only unsupported ones."""
    assert unsupported_reason(Path("D:/kb/a.mp4")) == "不支持的文件类型：.mp4"


def test_a_file_without_an_extension_says_so():
    assert "无扩展名" in unsupported_reason(Path("D:/kb/README"))


# --------------------------------------------------------------------------
# the two lists are told apart
# --------------------------------------------------------------------------


def test_the_old_binary_formats_are_the_unsupported_list_not_the_broken_one(
    temp_dir: Path,
):
    """格式不支持 ≠ 读不进来 — the user should not have to tell them apart."""
    legacy = write_bytes(temp_dir / "旧文档.doc", b"legacy")

    _, unsupported, _ = _pipeline().partition_workspace_files([legacy])
    result = {"files": [], "unsupported_files": unsupported}

    failed, total, unsupported_total = _collect_failed_files(result)

    assert total == 1
    assert unsupported_total == 1
    # They are still listed — not silently skipped (AC-E-004).
    assert len(failed) == 1
    assert failed[0]["path"] == str(legacy)
    assert "旧版二进制格式" in failed[0]["error"]


def test_a_broken_file_is_still_a_file_that_could_not_be_read():
    """The two lists do not swallow each other."""
    result = {
        "files": [
            {
                "path": "D:\\kb\\坏了.docx",
                "status": "error",
                "error": "ValueError('x')",
            },
            {"path": "D:\\kb\\好的.md", "status": "indexed"},
        ],
        "unsupported_files": [
            {
                "path": "D:\\kb\\旧文档.doc",
                "reason": unsupported_reason(Path("旧文档.doc")),
            },
        ],
    }

    failed, total, unsupported_total = _collect_failed_files(result)

    assert total == 2
    assert unsupported_total == 1
    # The broken one comes first — it is the one the user has to know about.
    assert failed[0]["path"] == "D:\\kb\\坏了.docx"
    assert failed[0]["error"] == "ValueError('x')"
    assert failed[1]["path"] == "D:\\kb\\旧文档.doc"


def test_the_old_binary_formats_are_not_hidden_by_the_cap():
    """A workspace with a hundred thousand code files in it.

    The list is capped, and the unsupported ones come last — so without
    a place of their own the 46 legacy files would be crowded out by
    the code files and never shown at all.
    """
    crowd = [
        {"path": f"D:\\kb\\{i}.py", "reason": unsupported_reason(Path(f"{i}.py"))}
        for i in range(500)
    ]
    legacy = [
        {
            "path": f"D:\\kb\\旧文档{i}.doc",
            "reason": unsupported_reason(Path(f"旧文档{i}.doc")),
        }
        for i in range(46)
    ]
    result = {"files": [], "unsupported_files": [*crowd, *legacy]}

    failed, total, unsupported_total = _collect_failed_files(result)

    assert len(failed) == MAX_FAILED_FILES
    assert total == 546
    assert unsupported_total == 546
    listed = [entry["path"] for entry in failed]
    assert sum(1 for path in listed if path.endswith(".doc")) == 46


def test_the_old_binary_formats_are_not_buried_by_the_broken_files():
    """The cap must not hide the files the user can do something about.

    The files that could not be read come first — there can be a
    hundred thousand of them — and the list is capped. Without a place
    of their own the old binary formats would be crowded out by the
    broken files and never shown at all (TASK-091).
    """
    broken = [
        {
            "path": f"D:\\kb\\坏了{i}.docx",
            "status": "error",
            "error": "ValueError('x')",
        }
        for i in range(400)
    ]
    legacy = [
        {
            "path": f"D:\\kb\\旧文档{i}.doc",
            "reason": unsupported_reason(Path(f"旧文档{i}.doc")),
        }
        for i in range(23)
    ]
    result = {"files": broken, "unsupported_files": legacy}

    failed, total, unsupported_total = _collect_failed_files(result)

    assert total == 423
    assert unsupported_total == 23
    assert len(failed) == MAX_FAILED_FILES
    listed = [entry["path"] for entry in failed]
    # The 23 old documents are still there — the cap did not eat them.
    assert sum(1 for path in listed if path.endswith(".doc")) == 23


def test_every_entry_says_which_list_it_belongs_to():
    """The backend says which list a file belongs to.

    The sidebar used to guess it from the wording of the reason, and
    the count and the list could disagree: a file that was skipped for
    its format counted as "could not be read" while it was shown under
    "a format nobody supports". The entries carry the tier now, and the
    reason of an unsupported file always begins with the marker.
    """
    result = {
        "files": [
            {
                "path": "D:\\kb\\坏了.docx",
                "status": "error",
                "error": "ValueError('x')",
            },
            {
                "path": "D:\\kb\\旧文档.doc",
                "status": "skipped",
                "reason": unsupported_reason(Path("旧文档.doc")),
            },
        ],
        "unsupported_files": [],
    }

    failed, total, unsupported_total = _collect_failed_files(result)

    assert total == 2
    # The skipped file is a format nobody supports — not a file that
    # could not be read. The two counts cannot disagree any more.
    assert unsupported_total == 1
    assert failed[0]["kind"] == "unreadable"
    assert failed[1]["kind"] == "unsupported"
    # The marker is the signal the sidebar reads.
    assert failed[1]["error"].startswith(UNSUPPORTED_MARKER)
    assert not failed[0]["error"].startswith(UNSUPPORTED_MARKER)


# --------------------------------------------------------------------------
# the build reports them, and goes on
# --------------------------------------------------------------------------


async def test_the_build_reports_them_instead_of_dropping_them(temp_dir: Path):
    """AC-E-004 — a file that is not read is still listed."""
    legacy = write_bytes(temp_dir / "旧文档.doc", b"legacy")
    pipeline = _pipeline()
    pipeline.workspace = _FakeWorkspace([legacy])

    result = await pipeline.build_workspace()

    assert result["unsupported"] == 1
    assert result["unsupported_files"][0]["path"] == str(legacy)
    assert "旧版二进制格式" in result["unsupported_files"][0]["reason"]
    # Nothing was read: the file was never tried.
    assert result["indexed"] == 0
    assert result["files"] == []


async def test_a_broken_file_is_recorded_and_the_rest_are_still_read(
    temp_dir: Path,
):
    """AC-E-005 — the reason is written down and the build goes on."""
    broken = write_bytes(temp_dir / "坏了.docx", b"this is not a docx")
    good = make_docx_file(temp_dir, "好的.docx")
    pipeline = _pipeline()

    result = await pipeline.ingest_file(broken)

    assert result["status"] == "error"
    assert result["error"], "the failure has a reason"

    # The build goes on: the next file is read and indexed.
    after = await pipeline.ingest_file(good)
    assert after["status"] == "indexed"


# --------------------------------------------------------------------------
# the folder builds report them too
# --------------------------------------------------------------------------


async def test_the_folder_build_reports_the_files_it_does_not_read(temp_dir: Path):
    """A folder build does not drop the old formats on the floor.

    The build of a folder is another road than the build of a
    workspace, and it used to end in another place: the files it did
    not read were simply not in the answer. A file that is not read is
    not a file that may be dropped (AC-E-004).
    """
    legacy = write_bytes(temp_dir / "旧文档.doc", b"legacy")

    result = await _pipeline().build_directory(temp_dir)

    assert [Path(item["path"]).name for item in result["unsupported_files"]] == [
        "旧文档.doc"
    ]
    assert "旧版二进制格式" in result["unsupported_files"][0]["reason"]
    # It was never tried: the build does not read what it cannot read.
    assert result["files"] == []


async def test_the_plain_folder_build_reports_them_too(temp_dir: Path):
    """The build that reads one file at a time says the same thing.

    The rest of the files are still read — the old formats do not stop
    the build, they are only reported.
    """
    legacy = write_bytes(temp_dir / "旧文档.doc", b"legacy")
    make_markdown_file(temp_dir, "note.md", "# 标题\n\n正文内容\n")

    result = await _pipeline().ingest_directory(temp_dir)

    assert [Path(item["path"]).name for item in result["unsupported_files"]] == [
        "旧文档.doc"
    ]
    assert result["indexed"] == 1


# --------------------------------------------------------------------------
# the single file route
# --------------------------------------------------------------------------


class _FakeApp:
    """An app with nothing on it — the pipeline reads the settings."""

    class state:
        settings = None


@pytest.fixture
async def store(tmp_path: Path):
    """A real store — the job state has to survive a restart."""
    from yuanwork.db.sqlite import SQLiteStore

    sqlite = SQLiteStore(tmp_path / "store.db")
    await sqlite.initialize()
    yield sqlite
    await sqlite.close()


async def test_a_single_file_this_release_does_not_read_is_reported(
    store, tmp_path: Path
):
    """The single-file route keeps the reason.

    The reason was computed and thrown away: the job said "completed"
    and nobody could tell why the file was not in the knowledge base
    (AC-E-004).
    """
    from yuanwork.api import deps
    from yuanwork.api.routes import ingest as ingest_routes

    legacy = write_bytes(tmp_path / "旧文档.doc", b"legacy")
    deps.set_sqlite_store(store)
    deps.set_semantic_memory(RecordingSemanticMemory())
    try:
        await ingest_routes._run_ingest_file_job(_FakeApp(), "job-1", legacy)
    finally:
        deps.reset_dependencies()

    job = await store.get_ingest_job("job-1")
    assert job["status"] == "completed"
    # It is a format nobody supports — not a file that could not be read.
    assert job["unsupported_total"] == 1
    assert job["failed_total"] == 1
    listed = json.loads(job["failed_files"])
    assert listed[0]["path"] == str(legacy)
    assert "旧版二进制格式" in listed[0]["error"]
    assert listed[0]["kind"] == "unsupported"
