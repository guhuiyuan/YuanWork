"""The evidence gate - the two gates that decide whether we may answer.

ADR-008: a hard rule gate and a model self-assessment gate, and the
more conservative of the two decides.

The hard rule gate catches *structural* insufficiency:

  - fewer than ``min_evidence`` pieces of evidence
  - a piece of evidence that cannot be pinned to a position in the
    original document

Evidence that cannot be located cannot be shown to the user, so it cannot
back a conclusion either - the user would have nothing to check the
conclusion against, and the whole promise of "每条结论都能核" rests on
the position being there.

The model gate catches *semantic* insufficiency: evidence that looks
relevant but does not answer the question. It arrives in TASK-010; until
then the gate runs on the hard rule alone, which is the degradation the
design allows (ASM-008: when the model's self-assessment is unreliable,
fall back to the hard rule gate and rather refuse more).

Both verdicts stay visible in the result. A refusal has to be
explainable, and "which gate said no" is the first thing worth knowing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from yuanwork.utils.anchor import has_anchor

logger = logging.getLogger(__name__)


#: The smallest number of evidence pieces that may back an answer.
DEFAULT_MIN_EVIDENCE = 2


#: The reasons a gate can give.
REASON_PASSED = "passed"
REASON_INSUFFICIENT = "insufficient_evidence"
REASON_UNLOCATABLE = "unlocatable_evidence"
REASON_MODEL_INSUFFICIENT = "model_insufficient"
REASON_MODEL_UNAVAILABLE = "model_unavailable"
REASON_GATE_ERROR = "gate_error"


@dataclass
class ModelAssessment:
    """What the model had to say about the evidence.

    ``failed`` separates the two cases the user must be told apart
    (design 4.3): the model said the evidence is not enough, or the
    model could not be reached at all. They lead to the same decision
    - refuse - but to different words.
    """

    sufficient: bool
    reason: str = ""
    failed: bool = False


#: A model gate: given the evidence and the question, what does the
#: model have to say about them?
ModelGate = Callable[[list[dict[str, Any]], str], Awaitable["ModelAssessment"]]


@dataclass
class HardRuleResult:
    """The verdict of the hard rule gate."""

    ok: bool
    reason: str
    count: int
    #: The ids of the evidence that could not be pinned to a position.
    unlocatable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "count": self.count,
            "unlocatable": list(self.unlocatable),
        }


@dataclass
class EvidenceGateResult:
    """The verdict of the whole gate (design 3.1 证据充分性判定).

    ``model_ok`` is True when the model gate raised no objection - which
    includes the case where it was not needed, because the hard rule had
    already decided. ``model_evaluated`` says which of the two it was.
    """

    passed: bool
    hard_rule_ok: bool
    model_ok: bool
    reason: str
    sufficient_count: int
    model_evaluated: bool = False
    model_failed: bool = False
    #: What the model had to say, in its own words.
    model_reason: str = ""
    unlocatable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "hard_rule_ok": self.hard_rule_ok,
            "model_ok": self.model_ok,
            "model_evaluated": self.model_evaluated,
            "model_failed": self.model_failed,
            "model_reason": self.model_reason,
            "reason": self.reason,
            "sufficient_count": self.sufficient_count,
            "unlocatable": list(self.unlocatable),
        }


def check_hard_rule(
    evidence: list[dict[str, Any]],
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
) -> HardRuleResult:
    """The hard rule gate: enough evidence, and every piece locatable.

    The two conditions are both structural, and both are cheap - no
    model is involved, so the gate costs nothing but a dictionary
    lookup per piece of evidence.
    """
    count = len(evidence)
    if count < min_evidence:
        return HardRuleResult(
            ok=False, reason=REASON_INSUFFICIENT, count=count
        )

    unlocatable: list[str] = []
    for index, chunk in enumerate(evidence, start=1):
        if not has_anchor(chunk):
            # A chunk with no id still has to be nameable, so the reader
            # can be told which piece of evidence was the problem.
            unlocatable.append(str(chunk.get("id") or f"#{index}"))

    if unlocatable:
        return HardRuleResult(
            ok=False,
            reason=REASON_UNLOCATABLE,
            count=count,
            unlocatable=unlocatable,
        )

    return HardRuleResult(ok=True, reason=REASON_PASSED, count=count)


class EvidenceGate:
    """The gate between the evidence and the answer.

    Usage::

        gate = EvidenceGate()
        result = await gate.evaluate(evidence, question)
        if not result.passed:
            ...  # refuse

    The model gate is optional. Without it the gate runs on the hard
    rule alone - the degradation ASM-008 allows.
    """

    def __init__(
        self,
        min_evidence: int = DEFAULT_MIN_EVIDENCE,
        model_gate: ModelGate | None = None,
    ):
        self.min_evidence = max(1, int(min_evidence))
        self.model_gate = model_gate

    def hard_rule(self, evidence: list[dict[str, Any]]) -> HardRuleResult:
        """The hard rule half of the verdict."""
        return check_hard_rule(evidence, self.min_evidence)

    async def evaluate(
        self,
        evidence: list[dict[str, Any]],
        question: str,
    ) -> EvidenceGateResult:
        """Decide whether the evidence may back an answer.

        The model gate is only asked when the hard rule has not already
        decided: a refusal the rules can settle does not need a model
        call, and AC-P-004 keeps the gates inside 1.5 seconds.
        """
        hard = self.hard_rule(evidence)

        model_ok = True
        model_evaluated = False
        model_failed = False
        model_reason = ""
        if self.model_gate is not None and hard.ok:
            model_evaluated = True
            try:
                assessment = await self.model_gate(evidence, question)
            except Exception:  # noqa: BLE001 - a gate that crashes must not answer
                # A model that cannot be reached cannot vouch for the
                # evidence either.
                logger.exception("The model gate crashed")
                assessment = ModelAssessment(
                    sufficient=False, reason="模型自评调用失败", failed=True
                )
            model_ok = bool(assessment.sufficient)
            model_failed = bool(assessment.failed)
            model_reason = assessment.reason

        passed = hard.ok and model_ok

        if passed:
            reason = REASON_PASSED
        elif not hard.ok:
            reason = hard.reason
        elif model_failed:
            # The model could not be reached. That is not the same as
            # "the documents are silent" (design 4.3) and the user is
            # told apart.
            reason = REASON_MODEL_UNAVAILABLE
        else:
            reason = REASON_MODEL_INSUFFICIENT

        return EvidenceGateResult(
            passed=passed,
            hard_rule_ok=hard.ok,
            model_ok=model_ok,
            model_evaluated=model_evaluated,
            model_failed=model_failed,
            model_reason=model_reason,
            reason=reason,
            sufficient_count=hard.count,
            unlocatable=list(hard.unlocatable),
        )
