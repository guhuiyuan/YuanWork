"""The model self-assessment gate - the second half of the evidence gate.

ADR-008: the hard rule catches what is structurally missing, and the
model catches what is structurally there but does not answer the
question. "These three passages are all about travel expenses" is not
an answer to "how many days do I have to submit the claim" - only the
model can tell the difference, and only it can say that the evidence
is beside the point.

The model is asked a narrow question and it answers with a JSON
verdict. Everything that is not a clear "yes" counts as a "no":

  - the call failed            → the model is unavailable
  - the reply cannot be parsed → the judgment failed
  - the model said "no"        → the evidence is insufficient

The gate errs towards refusing, because the alternative is an answer
the documents do not support - and the user has no way to check a
claim they cannot trace.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader

from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask
from yuanwork.privacy.redactor import Redactor
from yuanwork.retrieval.evidence_gate import ModelAssessment
from yuanwork.utils.chunk_meta import chunk_meta

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "orchestrator" / "prompts"
_jinja_env = Environment(loader=FileSystemLoader(str(_PROMPTS_DIR)), autoescape=False)


#: How long the model may take to judge the evidence. The gate has to
#: answer before the answer step can start, so a gate that hangs is a
#: gate that never answers - the wait is bounded. AC-P-004 puts the
#: refusal decision and the conflict decision together in 1.5 s; that
#: budget is a measured figure on the real provider, and this timeout
#: is the hard stop that keeps a slow model from stalling the question
#: indefinitely.
DEFAULT_GATE_TIMEOUT = 10.0

#: How much of the evidence goes to the model. A judge that reads ten
#: pages of a contract is neither fast nor reliable; the evidence that
#: matters is at the top of the ranking.
MAX_EVIDENCE_PIECES = 8

#: How much of one piece goes to the model.
MAX_CHARS_PER_PIECE = 1200


class ModelSelfAssessGate:
    """Asks the model whether the evidence answers the question.

    The evidence is redacted on its way to the model, like every other
    piece of text that leaves the machine.
    """

    def __init__(
        self,
        llm: LLMGateway | None,
        redactor: Redactor | None = None,
        timeout: float | None = None,
        max_pieces: int = MAX_EVIDENCE_PIECES,
        max_chars: int = MAX_CHARS_PER_PIECE,
    ):
        self.llm = llm
        self.redactor = redactor
        self.timeout = DEFAULT_GATE_TIMEOUT if timeout is None else timeout
        self.max_pieces = max_pieces
        self.max_chars = max_chars

    async def __call__(
        self,
        evidence: list[dict[str, Any]],
        question: str,
    ) -> ModelAssessment:
        """The gate calls the model gate; this is the model gate."""
        return await self.assess(evidence, question)

    async def assess(
        self,
        evidence: list[dict[str, Any]],
        question: str,
    ) -> ModelAssessment:
        """Ask the model whether the evidence answers the question."""
        if not evidence:
            # Nothing to judge - and nothing to answer from.
            return ModelAssessment(
                sufficient=False, reason="没有证据可判", failed=False
            )

        prompt = self._build_prompt(evidence, question)

        task = LLMTask(
            task_type="evidence_gate",
            messages=[
                LLMMessage(role="system", content=prompt),
                LLMMessage(role="user", content=question),
            ],
            temperature=0.0,
            max_tokens=200,
            json_mode=True,
            timeout=self.timeout,
        )

        try:
            response = await self.llm.complete(task)  # type: ignore[union-attr]
        except Exception as e:
            logger.warning(f"The evidence gate could not reach the model: {e}")
            return ModelAssessment(
                sufficient=False, reason="模型自评调用失败", failed=True
            )

        return self._parse(response.content)

    # ----- the prompt -----

    def _build_prompt(
        self,
        evidence: list[dict[str, Any]],
        question: str,
    ) -> str:
        template = _jinja_env.get_template("evidence_gate.j2")
        pieces = []
        for index, chunk in enumerate(evidence[: self.max_pieces], start=1):
            meta = chunk_meta(chunk)
            text = chunk.get("text", "")[: self.max_chars]
            if self.redactor is not None:
                text = self.redactor.redact(text)
            pieces.append({
                "index": index,
                "file_name": chunk.get("file_name", "unknown"),
                "location": _location_of(chunk, meta),
                "text": text,
            })

        return template.render(question=question, pieces=pieces)

    # ----- the verdict -----

    def _parse(self, content: str) -> ModelAssessment:
        """Read the model's verdict.

        Anything that is not a readable "yes" is a "no" - and a reply
        that cannot be read at all is a failure, not a refusal of the
        evidence.
        """
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.split("```")[1] if "```" in text[3:] else text[3:]
            if text.lower().startswith("json"):
                text = text[4:]
            text = text.strip()

        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("The evidence gate returned something unreadable")
            return ModelAssessment(
                sufficient=False, reason="模型自评结果无法解析", failed=True
            )

        if not isinstance(payload, dict):
            return ModelAssessment(
                sufficient=False, reason="模型自评结果格式不对", failed=True
            )

        sufficient = payload.get("sufficient")
        if not isinstance(sufficient, bool):
            logger.warning("The evidence gate returned no verdict")
            return ModelAssessment(
                sufficient=False, reason="模型自评没有给出结论", failed=True
            )

        reason = str(payload.get("reason") or "").strip()
        return ModelAssessment(sufficient=sufficient, reason=reason, failed=False)


def _location_of(chunk: dict[str, Any], meta: dict[str, Any]) -> str:
    """Where the piece sits in its document - the judge is told the
    position so it can tell two passages of the same document apart."""
    from yuanwork.utils.anchor import anchor_location, anchor_of

    anchor = anchor_of(chunk)
    if anchor is not None:
        location = anchor_location(anchor)
        if location:
            return location
    if meta.get("page") is not None:
        return f"第 {meta['page']} 页"
    return ""
