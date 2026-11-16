"""The build benchmark: a corpus, a profile, and a regression gate.

The numbers the performance promises are made of have to come from
somewhere, and it has to be somewhere repeatable. The synthetic corpus
is that place: a known shape, a fixed seed, the same bytes every time.
The real workspace says what the user's own documents look like - and
it is counted, not read.

* AC-P-001, AC-P-007, AC-P-010, AC-P-011, AC-P-012 - the build's
  numbers, and the promises they are measured against
* AC-F-026 - the build is done when the long tail is searchable
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# The benchmark scripts live outside the package - they are tools, not
# part of the product.
_SCRIPTS = Path(__file__).resolve().parents[4] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from bench.corpus import DEFAULT_PROFILE, StructureProfile, generate_corpus
from bench.workspace_profile import profile_workspace
from bench.run import (
    BASELINE_PATH,
    REGRESSION_TOLERANCE,
    TARGETS,
    check_targets,
    compare_with_baseline,
    load_baseline,
    write_baseline,
)


@pytest.fixture
def small_profile() -> StructureProfile:
    """A corpus small enough to build in a test."""
    return StructureProfile(
        name="test",
        counts={"pdf": 1, "pptx": 1, "docx": 1, "xlsx": 1, "md": 1, "png": 1},
        pages=2,
        paragraphs=2,
        tables=1,
        rows=3,
    )


# --------------------------------------------------------------------------
# the synthetic corpus
# --------------------------------------------------------------------------


def test_the_corpus_has_the_kinds_a_workspace_has(tmp_path: Path, small_profile):
    """Every kind the profile asks for is there."""
    result = generate_corpus(tmp_path / "corpus", small_profile)

    kinds = {entry["kind"] for entry in result["files"]}
    assert kinds == set(small_profile.counts)
    assert len(result["files"]) == sum(small_profile.counts.values())
    assert result["total_bytes"] > 0


def test_the_same_seed_gives_the_same_bytes(tmp_path: Path, small_profile):
    """Two runs have to measure the same work."""
    first = generate_corpus(tmp_path / "one", small_profile, seed=42)
    second = generate_corpus(tmp_path / "two", small_profile, seed=42)

    sizes_a = [entry["size_bytes"] for entry in first["files"]]
    sizes_b = [entry["size_bytes"] for entry in second["files"]]
    assert sizes_a == sizes_b


def test_a_different_seed_gives_a_different_corpus(tmp_path: Path, small_profile):
    first = generate_corpus(tmp_path / "one", small_profile, seed=1)
    second = generate_corpus(tmp_path / "two", small_profile, seed=2)

    # The shape is the same; the words are not.
    first_bytes = Path(first["files"][0]["path"]).read_bytes()
    second_bytes = Path(second["files"][0]["path"]).read_bytes()
    assert first_bytes != second_bytes


def test_the_corpus_is_not_kept_in_the_repository(tmp_path: Path, small_profile):
    """The dataset can be rebuilt - it does not belong in git."""
    result = generate_corpus(tmp_path / "corpus", small_profile)
    assert result["profile"]["name"] == "test"
    # Nothing was written outside the target directory.
    assert all(
        Path(entry["path"]).parent == tmp_path / "corpus"
        for entry in result["files"]
    )


def test_the_profile_says_what_the_corpus_looks_like(tmp_path: Path, small_profile):
    """The structure profile is what makes the numbers mean something."""
    generate_corpus(tmp_path / "corpus", small_profile)

    profile = profile_workspace(tmp_path / "corpus")

    assert profile["files"] == sum(small_profile.counts.values())
    assert profile["pages"] > 0
    assert profile["tables"] > 0
    assert profile["size_bytes"]["max"] > 0


# --------------------------------------------------------------------------
# the profiler counts, it does not read
# --------------------------------------------------------------------------


def test_the_profile_holds_no_text(tmp_path: Path, small_profile):
    """A profile is a number to compare against - not a copy of the
    documents."""
    generate_corpus(tmp_path / "corpus", small_profile)

    profile = profile_workspace(tmp_path / "corpus")

    # Every value is a number or a list of numbers.
    for key, value in profile.items():
        if key in ("root", "exists"):
            continue
        assert not isinstance(value, str) or key == "root", key
        if isinstance(value, dict):
            for inner in value.values():
                assert isinstance(inner, (int, float, str)), key


def test_the_profile_of_a_missing_workspace_says_so(tmp_path: Path):
    profile = profile_workspace(tmp_path / "nothing")
    assert profile["exists"] is False
    assert profile["files"] == 0


# --------------------------------------------------------------------------
# the regression gate
# --------------------------------------------------------------------------


def report(**throughput) -> dict:
    """A run's report."""
    return {
        "files": 100,
        "chunks": 1000,
        "elapsed_seconds": 60.0,
        "throughput": {
            "parse_files_per_minute": 120.0,
            "embed_chunks_per_second": 15.0,
            "write_chunks_per_second": 500.0,
            **throughput,
        },
    }


def test_the_parse_rate_is_what_the_build_delivers():
    """Four workers read at the same time - the build delivers four
    times what one core manages.

    The promise (AC-P-010) is about what the build delivers. A rate
    per core would say the build is four times slower than it is.
    """
    from bench.run import parse_rates

    rates = parse_rates(100, 20.0, 4)

    assert rates["parse_files_per_minute"] == 1200.0
    assert rates["parse_files_per_minute_per_core"] == 300.0


def test_one_worker_reads_at_the_same_rate_it_delivers():
    from bench.run import parse_rates

    rates = parse_rates(100, 20.0, 1)

    assert rates["parse_files_per_minute"] == rates[
        "parse_files_per_minute_per_core"
    ]


def test_a_slower_run_is_a_regression():
    baseline = report()
    slower = report(parse_files_per_minute=100.0)

    comparison = compare_with_baseline(slower, baseline)

    assert comparison["regression"] is True
    assert comparison["regressions"]


def test_a_run_that_is_almost_the_same_is_not_a_regression():
    """Ten percent is the line - a little slower is still fine."""
    baseline = report()
    slightly_slower = report(parse_files_per_minute=110.0)

    comparison = compare_with_baseline(slightly_slower, baseline)

    assert comparison["regression"] is False
    assert comparison["tolerance"] == REGRESSION_TOLERANCE


def test_a_faster_run_is_not_a_regression():
    baseline = report()
    faster = report(embed_chunks_per_second=30.0)

    assert compare_with_baseline(faster, baseline)["regression"] is False


def test_the_first_run_has_nothing_to_compare_with():
    comparison = compare_with_baseline(report(), None)
    assert comparison["regression"] is False
    assert "基线" in comparison["reason"]


def test_the_baseline_is_written_and_read_back(tmp_path: Path):
    path = tmp_path / "baseline.json"
    write_baseline(report(), path)

    loaded = load_baseline(path)
    assert loaded is not None
    assert loaded["throughput"]["parse_files_per_minute"] == 120.0


def test_the_baseline_lives_next_to_the_scripts():
    assert BASELINE_PATH.exists()
    assert BASELINE_PATH.name == "baseline.json"


def test_a_missing_baseline_is_not_an_error(tmp_path: Path):
    assert load_baseline(tmp_path / "nothing.json") is None


# --------------------------------------------------------------------------
# the promises the numbers are measured against
# --------------------------------------------------------------------------


def test_the_promises_have_their_numbers():
    """A promise without a number is not a promise."""
    assert TARGETS["end_to_end_minutes"] == 120.0
    assert TARGETS["longtail_minutes"] == 100.0
    assert TARGETS["parse_files_per_minute"] >= 120.0
    assert TARGETS["embed_chunks_per_second"] >= 15.0
    assert TARGETS["write_chunks_per_second"] >= 500.0


def test_a_run_that_keeps_the_promises_misses_nothing():
    assert check_targets(report()) == []


def test_a_run_that_is_too_slow_says_which_promise_it_broke():
    missed = check_targets(
        report(parse_files_per_minute=10.0, embed_chunks_per_second=1.0)
    )

    assert len(missed) == 2
    assert any("解析" in item for item in missed)
    assert any("嵌入" in item for item in missed)


def test_the_baseline_file_is_json():
    """The baseline is read by the next run - it has to be readable."""
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert "throughput" in data
