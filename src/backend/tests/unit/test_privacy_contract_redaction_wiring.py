"""TASK-005 — the redaction is actually wired in.

The redactor existed for a long time and was never called: the evidence
went to the model exactly as it was read from the documents. The promise
in the privacy document - that sensitive fields are masked before they
leave the machine - was not kept.

The redaction now happens on the way *to* the model: the answer prompt
and the conflict judgement get redacted evidence, while the citation the
user reads stays as it is - it is their own data, and it stays local.

* AC-F-019 — nothing sensitive leaves the machine
* the switch that turns the redaction off
* the entity extraction prompt, whose JSON example used to break
  `str.format`
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from yuanwork.orchestrator.steps.answer import _build_answer_task
from yuanwork.privacy.redactor import Redactor


SECRET_PHONE = "13812345678"
SECRET_EMAIL = "zhangsan@example.com"
SECRET_ID = "11010119900307551X"


def evidence_with(text: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "c1",
            "doc_id": "d1",
            "text": text,
            "file_name": "通讯录.txt",
            "file_path": "/kb/通讯录.txt",
        }
    ]


def prompt_of(task) -> str:
    return "\n".join(m.content for m in task.messages)


# --------------------------------------------------------------------------
# the answer prompt
# --------------------------------------------------------------------------


def test_the_answer_prompt_redacts_the_evidence():
    text = f"联系人 {SECRET_PHONE}，邮箱 {SECRET_EMAIL}"

    task = _build_answer_task(
        query="谁是我的联系人？",
        evidence=evidence_with(text),
        confidence={},
        redactor=Redactor(enabled=True),
    )

    prompt = prompt_of(task)
    assert SECRET_PHONE not in prompt
    assert SECRET_EMAIL not in prompt
    assert "[手机号]" in prompt
    assert "[邮箱]" in prompt


def test_the_answer_prompt_is_untouched_when_the_redactor_is_off():
    text = f"联系人 {SECRET_PHONE}"

    task = _build_answer_task(
        query="谁是我的联系人？",
        evidence=evidence_with(text),
        confidence={},
        redactor=Redactor(enabled=False),
    )

    assert SECRET_PHONE in prompt_of(task)


def test_no_redactor_means_no_redaction():
    """A caller that does not pass one gets the plain text."""
    task = _build_answer_task(
        query="问题",
        evidence=evidence_with(f"电话 {SECRET_PHONE}"),
        confidence={},
    )

    assert SECRET_PHONE in prompt_of(task)


def test_the_evidence_itself_is_not_changed():
    """The citation the user reads is their own data - it stays as it is."""
    evidence = evidence_with(f"电话 {SECRET_PHONE}")

    _build_answer_task(
        query="问题",
        evidence=evidence,
        confidence={},
        redactor=Redactor(enabled=True),
    )

    assert SECRET_PHONE in evidence[0]["text"]


def test_the_id_card_is_masked_too():
    text = f"身份证 {SECRET_ID}"

    task = _build_answer_task(
        query="问题",
        evidence=evidence_with(text),
        confidence={},
        redactor=Redactor(enabled=True),
    )

    assert SECRET_ID not in prompt_of(task)
    assert "[身份证号]" in prompt_of(task)


# --------------------------------------------------------------------------
# the conflict judgement
# --------------------------------------------------------------------------


async def test_the_conflict_judgement_gets_redacted_evidence():
    """The second place where knowledge base text leaves the machine."""
    from yuanwork.consolidation.conflict import detect_conflict

    sent: dict[str, Any] = {}

    class FakeLLM:
        async def complete(self, task):
            sent["prompt"] = "\n".join(m.content for m in task.messages)
            return type("R", (), {"content": '{"verdict": "consistent"}'})()

    await detect_conflict(
        llm=FakeLLM(),
        llm_answer="答案",
        kb_evidence=f"证据里的电话 {SECRET_PHONE}",
        redactor=Redactor(enabled=True),
    )

    assert SECRET_PHONE not in sent["prompt"]
    assert "[手机号]" in sent["prompt"]


# --------------------------------------------------------------------------
# the switch
# --------------------------------------------------------------------------


def test_the_settings_have_the_switch_and_it_is_on_by_default():
    from yuanwork.config import YuanWorkSettings

    settings = YuanWorkSettings(data_dir=Path("."))
    assert settings.enable_redaction is True


def test_the_switch_can_be_turned_off():
    from yuanwork.config import YuanWorkSettings

    settings = YuanWorkSettings(data_dir=Path("."), enable_redaction=False)
    assert settings.enable_redaction is False


def test_the_chat_route_builds_a_redactor_from_the_settings():
    """The route wires the switch into the agent."""
    from fastapi import FastAPI

    from yuanwork.api.routes.chat import _build_redactor

    app = FastAPI()
    app.state.settings = type("S", (), {"enable_redaction": True})()
    request = type("R", (), {"app": app})()

    assert _build_redactor(request).enabled is True

    app.state.settings = type("S", (), {"enable_redaction": False})()
    assert _build_redactor(request).enabled is False


# --------------------------------------------------------------------------
# the entity extraction prompt
# --------------------------------------------------------------------------


def test_the_entity_extraction_prompt_can_be_formatted():
    """Its JSON example used to be read as a placeholder."""
    from yuanwork.wiki.entity_extractor import ENTITY_EXTRACTION_PROMPT

    prompt = ENTITY_EXTRACTION_PROMPT.format(text="一些正文")

    assert "一些正文" in prompt
    # the JSON example reaches the model intact
    assert '"entities"' in prompt
    assert "{{" not in prompt
    assert "}}" not in prompt


def test_the_entity_extraction_prompt_has_one_placeholder():
    """One placeholder, and every other brace is escaped."""
    from yuanwork.wiki.entity_extractor import ENTITY_EXTRACTION_PROMPT

    body = ENTITY_EXTRACTION_PROMPT.replace("{{", "").replace("}}", "")

    assert body.count("{") == 1
    assert body.count("}") == 1
    assert "{text}" in body
