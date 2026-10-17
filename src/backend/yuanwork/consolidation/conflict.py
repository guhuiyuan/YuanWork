"""Conflict resolution between LLM training knowledge and KB evidence."""

from __future__ import annotations

import json
import logging
from typing import Any

from jinja2 import Environment, FileSystemLoader
from pathlib import Path

from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "orchestrator" / "prompts"
_jinja_env = Environment(loader=FileSystemLoader(str(_PROMPTS_DIR)), autoescape=False)


async def detect_conflict(
    llm: LLMGateway,
    llm_answer: str,
    kb_evidence: str,
) -> dict[str, Any]:
    """Detect if LLM answer contradicts KB evidence.

    Returns dict with:
    - verdict: 'consistent' | 'conflict' | 'unrelated'
    - conflict_claims: list of {topic, llm_says, kb_says}
    - explanation: str
    """
    template = _jinja_env.get_template("conflict.j2")
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
        return json.loads(content)
    except Exception as e:
        logger.warning(f"Conflict detection failed: {e}")
        return {
            "verdict": "unrelated",
            "conflict_claims": [],
            "explanation": f"Detection failed: {e}",
        }


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
