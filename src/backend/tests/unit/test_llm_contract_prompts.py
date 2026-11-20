"""The four prompt templates.

The prompts carry the product promises — "every fact must be cited",
"say 'not enough evidence' when it is", "flag a conflict" — and none of
them are enforced in code. So the *text* is the contract, and it is
pinned here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROMPTS = Path(__file__).resolve().parents[2] / "yuanwork" / "orchestrator" / "prompts"


def read(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def understand() -> str:
    return read("understand.j2")


@pytest.fixture(scope="module")
def answer() -> str:
    return read("answer.j2")


@pytest.fixture(scope="module")
def conflict() -> str:
    return read("conflict.j2")


@pytest.fixture(scope="module")
def refusal() -> str:
    return read("refusal.j2")


# --------------------------------------------------------------------------
# the understanding template
# --------------------------------------------------------------------------


def test_the_understanding_template_demands_json_only(understand):
    assert "Output ONLY valid JSON" in understand
    assert "no prose before or after" in understand


def test_the_understanding_template_forbids_hallucinating(understand):
    assert "Do not hallucinate" in understand


def test_the_understanding_template_declares_the_seven_fields(understand):
    for field in (
        "intent", "topic", "file_hint", "time_hint", "entity_hints",
        "requires_evidence", "needs_refusal_if_no_match",
    ):
        assert f'"{field}"' in understand


def test_the_understanding_template_takes_the_query(understand):
    assert "{{ query }}" in understand


# --------------------------------------------------------------------------
# the answer template
# --------------------------------------------------------------------------


def test_the_answer_template_has_six_core_rules(answer):
    rules = [
        line for line in answer.splitlines()
        if line.strip().startswith(("1. **", "2. **", "3. **", "4. **", "5. **", "6. **"))
    ]
    assert len(rules) == 6


def test_the_answer_template_demands_citation_numbers(answer):
    assert "每个事实必须用 [1][2][3] 等引用编号标注" in answer


def test_the_answer_template_forbids_inventing(answer):
    assert "禁止编造" in answer


def test_the_answer_template_demands_the_page_number(answer):
    assert "第 N 页" in answer
    assert "Slide N" in answer


def test_the_answer_template_demands_an_admission_of_gaps(answer):
    assert "证据不足" in answer


def test_the_answer_template_demands_the_conflict_marker(answer):
    assert "⚠️ 注意：通用知识与知识库存在分歧" in answer


def test_the_answer_template_lists_the_evidence(answer):
    assert "{% for chunk in evidence %}" in answer
    assert "来源：《{{ chunk.file_name }}》" in answer
    assert "{{ chunk.text }}" in answer


def test_the_answer_template_takes_the_query(answer):
    assert "{{ query }}" in answer


def test_the_answer_template_states_the_output_requirements(answer):
    assert "使用 Markdown 格式" in answer
    assert "重要事实后紧跟引用编号" in answer
    assert "未在知识库中找到明确证据" in answer


def test_the_answer_template_does_not_use_the_confidence(answer):
    """The confidence is passed to the renderer but the template ignores
    it."""
    assert "{{ confidence }}" not in answer


def test_the_answer_template_states_the_range_of_the_numbers(answer):
    """The model has to know how many references there are.

    The model renumbered the sources in its own "引用来源" list once, and
    the number it made up was above the evidence - which the citation
    check (ADR-009) then rejects, so a question that could be answered
    came back as a refusal (TASK-087)."""
    assert "{{ evidence|length }}" in answer
    assert "只能引用上面列出的编号" in answer
    assert "不要自己重新编号" in answer


# --------------------------------------------------------------------------
# the conflict template
# --------------------------------------------------------------------------


def test_the_conflict_template_demands_json_only(conflict):
    assert "Output ONLY valid JSON" in conflict


def test_the_conflict_template_declares_the_three_verdicts(conflict):
    assert "consistent" in conflict
    assert "conflict" in conflict
    assert "unrelated" in conflict


def test_the_conflict_template_uses_the_agent_field_names(conflict):
    """The template asks for `llm_says` / `kb_says` — which is *not* what
    the response model expects."""
    assert '"llm_says"' in conflict
    assert '"kb_says"' in conflict


def test_the_conflict_template_takes_the_answer_and_the_evidence(conflict):
    assert "{{ llm_answer }}" in conflict
    assert "{{ kb_evidence }}" in conflict


# --------------------------------------------------------------------------
# the refusal template
# --------------------------------------------------------------------------


def test_the_refusal_template_leaves_the_opening_to_the_code(refusal):
    """The wording depends on the reason the gate gave, so the opening
    is rendered in - not written into the template."""
    assert "{{ opening }}" in refusal


def test_the_refusal_template_lists_the_likely_locations(refusal):
    assert "最可能相关的位置" in refusal
    assert "{% for position in positions %}" in refusal
    assert "{{ position.file_name }}" in refusal
    assert "{{ position.location }}" in refusal


def test_the_refusal_template_offers_four_ways_forward(refusal):
    """The first suggestion depends on the reason; the other three are
    the same for every refusal."""
    for number in ("2. **", "3. **", "4. **"):
        assert number in refusal
    assert "1. **" in refusal


def test_the_refusal_template_says_what_to_do_for_each_reason(refusal):
    for reason in (
        "unlocatable_evidence",
        "model_insufficient",
        "model_unavailable",
    ):
        assert reason in refusal


def test_the_refusal_template_hides_its_heading(refusal):
    """The title is a Jinja comment, so it never reaches the user."""
    assert refusal.lstrip().startswith("{#")
    assert "Refusal Response Template" in refusal


def test_the_refusal_template_is_not_a_model_prompt(refusal):
    """It is rendered directly — there is no model call."""
    assert "You are" not in refusal
    assert "Output" not in refusal


# --------------------------------------------------------------------------
# the templates are the only ones
# --------------------------------------------------------------------------


def test_there_are_exactly_six_templates():
    names = sorted(p.name for p in PROMPTS.glob("*.j2"))
    assert names == [
        "answer.j2",
        "conflict.j2",
        "evidence_gate.j2",
        "refusal.j2",
        "refusal_empty.j2",
        "understand.j2",
    ]
