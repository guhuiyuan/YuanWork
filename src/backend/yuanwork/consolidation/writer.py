"""Write control - manages what gets stored in episodic memory."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask

logger = logging.getLogger(__name__)


# Prompt templates
EPISODE_SUMMARY_PROMPT = """\
你是一个对话摘要助手。请将以下对话回合浓缩为 100 字以内的摘要。

要求：
1. 保留关键事实、决定、问题
2. 提取 3-5 个主题标签（JSON 数组）
3. 提取涉及的主要实体（文件名、人物、项目、概念）

输出 JSON 格式：
{
  "summary": "string",
  "topic_tags": ["tag1", "tag2"],
  "entities": ["entity1", "entity2"],
  "importance_score": 0.0-1.0
}

对话：
{conversation}
"""


class WriteController:
    """Controls what gets written to episodic memory."""

    def __init__(self, llm_gateway: LLMGateway | None = None):
        self.llm = llm_gateway

    async def summarize_episode(
        self, conversation: str
    ) -> dict[str, Any]:
        """Summarize a conversation turn for storage."""
        if self.llm is None:
            return self._fallback_summary(conversation)

        try:
            prompt = EPISODE_SUMMARY_PROMPT.format(conversation=conversation)
            task = LLMTask(
                task_type="reflection",
                messages=[LLMMessage(role="user", content=prompt)],
                temperature=0.0,
                json_mode=True,
            )
            response = await self.llm.complete(task)
            import json

            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            return json.loads(content)
        except Exception as e:
            logger.warning(f"Episode summarization failed: {e}")
            return self._fallback_summary(conversation)

    def _fallback_summary(self, conversation: str) -> dict[str, Any]:
        """Fallback summary without LLM."""
        return {
            "summary": conversation[:100],
            "topic_tags": [],
            "entities": [],
            "importance_score": 0.5,
        }
