"""TASK-002 — the request and response model constraints.

* AC-F-003 — the chat request's field constraints
* AC-F-004 — the chat response's field constraints
* AC-F-005 — the file type enumeration
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from yuanwork.api.schemas import (
    ChatRequest,
    ChatResponse,
    Citation,
    ConflictInfo,
    FileType,
    IngestStatusResponse,
)


# --------------------------------------------------------------------------
# AC-F-003 — the request model
# --------------------------------------------------------------------------


def test_a_query_is_required():
    with pytest.raises(ValidationError):
        ChatRequest()


def test_an_empty_query_is_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(query="")


def test_a_query_of_8000_characters_is_accepted():
    assert ChatRequest(query="x" * 8000).query == "x" * 8000


def test_a_query_of_8001_characters_is_rejected():
    with pytest.raises(ValidationError):
        ChatRequest(query="x" * 8001)


def test_the_optional_fields_have_empty_defaults():
    req = ChatRequest(query="hello")
    assert req.session_id is None
    assert req.follow_up is None
    assert req.opts == {}


def test_a_session_id_and_a_follow_up_are_accepted():
    """The follow-up carries the previous positions and the reader's
    own question - **not** the previous answer (ADR-057)."""
    req = ChatRequest(
        query="hello",
        session_id="abc",
        follow_up={
            "question": "上一句问话",
            "positions": [{"file_name": "采购合同.pdf", "page": 3}],
        },
        opts={"k": "v"},
    )
    assert req.session_id == "abc"
    assert req.follow_up is not None
    assert req.follow_up.question == "上一句问话"
    assert req.follow_up.positions[0].file_name == "采购合同.pdf"
    assert req.follow_up.positions[0].page == 3
    assert req.opts == {"k": "v"}


def test_a_follow_up_is_bounded():
    """The prompt must not grow with the chat (ADR-057)."""
    with pytest.raises(ValidationError):
        ChatRequest(
            query="hello",
            follow_up={"question": "x" * 501, "positions": []},
        )
    with pytest.raises(ValidationError):
        ChatRequest(
            query="hello",
            follow_up={
                "question": "问话",
                "positions": [{"file_name": f"f{i}.pdf"} for i in range(11)],
            },
        )


# --------------------------------------------------------------------------
# AC-F-004 — the response model
# --------------------------------------------------------------------------


def test_a_confidence_below_zero_is_rejected():
    with pytest.raises(ValidationError):
        ChatResponse(
            session_id="s", answer="a", confidence=-0.1,
        )


def test_a_confidence_above_one_is_rejected():
    with pytest.raises(ValidationError):
        ChatResponse(session_id="s", answer="a", confidence=1.1)


def test_a_confidence_in_range_is_accepted():
    for value in (0.0, 0.5, 1.0):
        assert ChatResponse(session_id="s", answer="a", confidence=value).confidence == value


def test_the_optional_response_fields_default_to_empty():
    resp = ChatResponse(session_id="s", answer="a", confidence=0.5)
    assert resp.citations == []
    assert resp.conflicts == []
    assert resp.trace == []
    assert resp.refused is False
    assert resp.refusal_reason is None
    assert resp.wiki_compile is None


def test_a_citation_carries_the_location_fields():
    cite = Citation(
        index=1,
        file_name="a.pdf",
        file_path="/tmp/a.pdf",
        text="原文",
        chunk_id="c1",
    )
    assert cite.page is None
    assert cite.slide_number is None
    assert cite.section is None
    assert cite.index == 1


# --------------------------------------------------------------------------
# AC-F-005 — the file type enumeration
# --------------------------------------------------------------------------


def test_the_file_type_values():
    assert {ft.value for ft in FileType} == {
        "pdf", "docx", "pptx", "xlsx", "md", "txt", "image", "unknown",
    }


def test_an_unknown_file_type_value_is_rejected():
    """`FileType` is a plain enum, so an unknown value raises ValueError
    rather than a pydantic ValidationError."""
    with pytest.raises(ValueError):
        FileType("exe")


# --------------------------------------------------------------------------
# extra: the ingest status model
# --------------------------------------------------------------------------


def test_the_ingest_status_progress_is_a_ratio():
    with pytest.raises(ValidationError):
        IngestStatusResponse(job_id="j", status="running", progress=1.5)
    with pytest.raises(ValidationError):
        IngestStatusResponse(job_id="j", status="running", progress=-0.1)
    ok = IngestStatusResponse(job_id="j", status="running", progress=0.5)
    assert ok.progress == 0.5


def test_the_ingest_status_states():
    for state in ("pending", "running", "completed", "failed"):
        assert IngestStatusResponse(job_id="j", status=state).status == state
    with pytest.raises(ValidationError):
        IngestStatusResponse(job_id="j", status="cancelled")


# --------------------------------------------------------------------------
# extra: the conflict model — the field names the agent does not produce
# --------------------------------------------------------------------------


def test_the_conflict_model_expects_llm_answer_and_kb_evidence():
    conflict = ConflictInfo(type="llm_vs_kb")
    assert conflict.llm_answer == ""
    assert conflict.kb_evidence == ""
    assert conflict.message == ""
    assert conflict.topic is None


def test_the_conflict_type_is_restricted():
    with pytest.raises(ValidationError):
        ConflictInfo(type="something_else")
