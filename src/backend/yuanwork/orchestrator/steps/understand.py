"""Step 1: Query Understanding - parse user query into structured intent."""

from __future__ import annotations

import json
import logging
from typing import Any

from jinja2 import Environment, FileSystemLoader
from pathlib import Path

from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask

logger = logging.getLogger(__name__)

# Load Jinja2 environment
_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_jinja_env = Environment(loader=FileSystemLoader(str(_PROMPTS_DIR)), autoescape=False)


async def understand_query(
    llm: LLMGateway,
    query: str,
    follow_up: str = "",
) -> dict[str, Any]:
    """Parse the user query into structured understanding.

    ``follow_up`` is the previous turn's positions and the reader's own
    question - **never the previous answer** (ADR-057). It is there so
    the model can tell whether this question is a follow-up ("再详细点",
    "它") and, if it is, which file it is about.

    Returns:
        Dict with keys: intent, topic, file_hint, time_hint, entity_hints,
                        requires_evidence, needs_refusal_if_no_match,
                        is_follow_up
    """
    template = _jinja_env.get_template("understand.j2")
    prompt = template.render(query=query, follow_up=follow_up)

    task = LLMTask(
        task_type="understand",
        messages=[
            LLMMessage(role="system", content=prompt),
            LLMMessage(role="user", content=query),
        ],
        temperature=0.0,
        json_mode=True,
    )

    try:
        response = await llm.complete(task)
        # Parse JSON
        content = response.content.strip()
        # Strip code fences if present
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()

        result = json.loads(content)
        # Ensure required fields
        return {
            "intent": result.get("intent", "general"),
            "topic": result.get("topic", ""),
            "file_hint": result.get("file_hint"),
            "time_hint": result.get("time_hint"),
            "entity_hints": result.get("entity_hints", []),
            "requires_evidence": result.get("requires_evidence", True),
            "needs_refusal_if_no_match": result.get("needs_refusal_if_no_match", True),
            "is_follow_up": bool(result.get("is_follow_up", False)),
        }
    except Exception as e:
        logger.warning(f"Query understanding failed, using defaults: {e}")
        # Fallback: return minimal understanding
        return {
            "intent": "general",
            "topic": query[:50],
            "file_hint": None,
            "time_hint": None,
            "entity_hints": [],
            "requires_evidence": True,
            "needs_refusal_if_no_match": True,
            # Without the model's judgement there is no follow-up - the
            # question is read on its own, exactly as before.
            "is_follow_up": False,
        }
