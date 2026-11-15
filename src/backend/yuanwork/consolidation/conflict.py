"""Conflict resolution between LLM training knowledge and KB evidence."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from jinja2 import Environment, FileSystemLoader
from pathlib import Path

from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask
from yuanwork.privacy.redactor import Redactor

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "orchestrator" / "prompts"
_jinja_env = Environment(loader=FileSystemLoader(str(_PROMPTS_DIR)), autoescape=False)


#: The wording that turns an answer into a statement of fact.
#:
#: "The answer must be checked" is only worth a model call when the
#: answer says something the documents could contradict. These are the
#: words that make it a statement.
NORMATIVE_WORDS = (
    "规定",
    "应当",
    "必须",
    "不得",
    "禁止",
    "至少",
    "最多",
    "不超过",
    "不低于",
    "适用于",
    "有效期",
    "生效",
    "失效",
)

#: The subjects that make a question high risk.
#:
#: A question about rules, contracts or money is a question whose
#: answer the user will act on - so it is always checked.
HIGH_RISK_SUBJECTS = (
    "制度",
    "规章",
    "规范",
    "流程",
    "标准",
    "合同",
    "契约",
    "协议",
    "条款",
    "金额",
    "费用",
    "价格",
    "报价",
    "付款",
    "结算",
    "报销",
    "预算",
    "上限",
    "下限",
    "阈值",
)

#: A number in the answer - a date, an amount, a count.
_DIGIT = re.compile(r"\d")

#: A number written in Chinese characters - 三百元、第十条、百分之五.
#:
#: The characters on their own mean nothing (一旦, 第三方, 十分), so a
#: number needs a unit or an ordinal around it before it counts.
_CHINESE_NUMBER = re.compile(
    r"第[零一二三四五六七八九十百千万两]+[条章节款项]"
    r"|百分之[零一二三四五六七八九十百千万两]+"
    r"|分之[零一二三四五六七八九十百千万两]+"
    r"|[零一二三四五六七八九十百千万两]+[元块天日月次人个份成折倍项]"
)


def has_number(text: str) -> bool:
    """Whether the text says a number - in digits or in characters."""
    if not text:
        return False
    return bool(_DIGIT.search(text) or _CHINESE_NUMBER.search(text))


class ConflictJudge:
    """Decides whether an answer has to be checked against the documents.

    Checking every answer costs a model call and a second of waiting
    (AC-P-004). An answer that says nothing specific - and a question
    that is not about rules, contracts or money - cannot contradict the
    documents in a way the user would act on. Those are skipped, and the
    reason is written down (AC-F-029).

    But when in doubt, the check runs: a rule that cannot be read is a
    rule that must not skip the check.
    """

    def should_judge(self, answer: str, question: str) -> dict[str, Any]:
        """Whether this answer has to be checked, and why.

        Returns:
            ``judge`` - whether the check runs.
            ``reason`` - why, in words the interface can show.
            ``matched`` - the rules that fired.
        """
        try:
            return self._decide(answer or "", question or "")
        except Exception as e:  # noqa: BLE001 - a broken rule must not skip the check
            logger.warning(f"The conflict pre-check failed: {e}")
            return {
                "judge": True,
                "reason": "前置规则判断异常，按需要判定处理",
                "matched": [],
            }

    def _decide(self, answer: str, question: str) -> dict[str, Any]:
        """The rules themselves."""
        matched: list[str] = []

        if has_number(answer):
            # A number, a date, an amount - a statement that can be
            # contradicted.
            matched.append("answer_has_number")

        hit = next((w for w in NORMATIVE_WORDS if w in answer), None)
        if hit:
            matched.append(f"answer_normative:{hit}")

        subject = next((w for w in HIGH_RISK_SUBJECTS if w in question), None)
        if subject:
            matched.append(f"question_subject:{subject}")

        if matched:
            return {
                "judge": True,
                "reason": "命中具体断言或高风险主题：" + "、".join(matched),
                "matched": matched,
            }

        return {
            "judge": False,
            "reason": "答案不含具体断言，所问内容也不涉及制度、合同与金额",
            "matched": [],
        }


async def detect_conflict(
    llm: LLMGateway,
    llm_answer: str,
    kb_evidence: str,
    redactor: "Redactor | None" = None,
) -> dict[str, Any]:
    """Detect if LLM answer contradicts KB evidence.

    Returns dict with:
    - verdict: 'consistent' | 'conflict' | 'unrelated'
    - conflict_claims: list of {topic, llm_says, kb_says}
    - explanation: str

    The evidence is redacted before it is handed to the model, like every
    other piece of text that leaves the machine.
    """
    template = _jinja_env.get_template("conflict.j2")
    if redactor is not None:
        kb_evidence = redactor.redact(kb_evidence)
    prompt = template.render(llm_answer=llm_answer, kb_evidence=kb_evidence)

    try:
        task = LLMTask(
            task_type="conflict_check",
            messages=[
                LLMMessage(role="system", content=prompt),
                LLMMessage(role="user", content=f"LLM: {llm_answer}\n\nKB: {kb_evidence}"),
            ],
            temperature=0.0,
            json_mode=True,
        )
        response = await llm.complete(task)
        content = response.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        verdict = json.loads(content)
        if isinstance(verdict, dict):
            verdict.setdefault("failed", False)
        return verdict
    except Exception as e:
        logger.warning(f"Conflict detection failed: {e}")
        # The check did not run. That is not the same as "there is no
        # conflict" - and the caller has to be able to tell the two
        # apart (AC-E-010).
        return {
            "verdict": "unrelated",
            "conflict_claims": [],
            "explanation": "Conflict detection could not be completed",
            "failed": True,
        }


def rewrite_with_knowledge_base(
    model_answer: str,
    evidence: list[dict[str, Any]],
    citations: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    check_failed: bool = False,
) -> str:
    """The answer, taken from the knowledge base.

    When the model's answer contradicts the documents, the documents
    are the answer (ADR-010). The model's own wording is kept as a
    comparison at the end - the user needs to see what it said to
    judge the difference - but it does not stand as the answer.

    When the check itself could not be completed, the model's answer
    is not shown at all: it has not been checked against anything, so
    it does not get to stand as a conclusion either (AC-E-010).
    """
    parts: list[str] = []

    if check_failed:
        parts.append(
            "⚠ **注意：未能核对答案与知识库是否一致**\n\n"
            "下面按知识库原文给出。"
        )
    else:
        notice = format_conflict_message(
            {"verdict": "conflict", "conflict_claims": conflicts}
        )
        if notice:
            parts.append(notice)

    parts.append("## 以知识库为准\n")
    for citation in citations:
        index = int(citation.get("index", 0)) - 1
        chunk = evidence[index] if 0 <= index < len(evidence) else {}
        text = (chunk.get("text") or "").strip()
        location = citation.get("location") or ""
        where = f"（{location}）" if location else ""
        parts.append(
            f"[{citation.get('index')}] 《{citation.get('file_name', '')}》{where}\n\n{text}"
        )

    if not check_failed and (model_answer or "").strip():
        parts.append(
            "## 模型的原答（与知识库不一致，仅作对照）\n\n" + model_answer
        )

    return "\n\n".join(parts)


def format_conflict_message(conflict: dict[str, Any]) -> str:
    """Format a conflict as a user-facing warning message."""
    if conflict.get("verdict") != "conflict":
        return ""

    claims = conflict.get("conflict_claims", [])
    if not claims:
        return "⚠️ 注意：通用知识与知识库存在分歧。"

    lines = ["⚠️ **注意：通用知识与知识库存在分歧**", ""]
    for i, claim in enumerate(claims, 1):
        lines.append(f"**冲突点 {i}：{claim.get('topic', 'N/A')}**")
        lines.append(f"- 知识库说：{claim.get('kb_says', 'N/A')}")
        lines.append(f"- 通用模型说：{claim.get('llm_says', 'N/A')}")
        lines.append("")

    lines.append("已以知识库为准，请确认是否需要更新知识库。")
    return "\n".join(lines)
