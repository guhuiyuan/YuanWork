"""Step 4: Answer Generation - synthesize final answer with citations."""

from __future__ import annotations

import logging
from typing import Any

from jinja2 import Environment, FileSystemLoader
from pathlib import Path

from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_jinja_env = Environment(loader=FileSystemLoader(str(_PROMPTS_DIR)), autoescape=False)


async def generate_answer(
    llm: LLMGateway,
    query: str,
    evidence: list[dict[str, Any]],
    confidence: dict[str, Any],
) -> str:
    """Generate final answer with mandatory citations.

    The prompt enforces:
    - Citation [n] format
    - No fabrication
    - Refusal when evidence insufficient
    """
    template = _jinja_env.get_template("answer.j2")

    # Format evidence for prompt
    formatted_evidence = []
    for chunk in evidence:
        meta = chunk.get("meta", {})
        formatted_evidence.append({
            "file_name": chunk.get("file_name", "unknown"),
            "meta": meta,
            "text": chunk.get("text", "")[:2000],  # Truncate very long chunks
        })

    prompt = template.render(
        query=query,
        evidence=formatted_evidence,
        confidence=confidence,
    )

    task = LLMTask(
        task_type="answer",
        messages=[
            LLMMessage(role="system", content=prompt),
            LLMMessage(role="user", content=query),
        ],
        temperature=0.1,
        max_tokens=2000,
    )

    response = await llm.complete(task)
    return response.content


async def generate_refusal(
    candidates: list[dict[str, Any]],
) -> str:
    """Generate a refusal message listing most likely locations."""
    template = _jinja_env.get_template("refusal.j2")

    formatted = []
    for c in candidates[:5]:
        meta = c.get("meta", {})
        formatted.append({
            "file_name": c.get("file_name", "unknown"),
            "meta": meta,
        })

    return template.render(candidates=formatted)
