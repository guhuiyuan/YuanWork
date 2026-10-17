"""Entity Extractor - NER for people, projects, concepts, etc."""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask

if TYPE_CHECKING:
    from yuanwork.db.sqlite import SQLiteStore


logger = logging.getLogger(__name__)


ENTITY_EXTRACTION_PROMPT = """\
你是一个命名实体识别（NER）助手。从以下文本中抽取实体。

## 实体类型
- person: 人物
- project: 项目/产品
- concept: 概念/术语
- contract: 合同/协议
- law: 法律法规
- org: 组织/公司
- location: 地点
- other: 其他

## 输出 JSON
{
  "entities": [
    {
      "name": "实体名",
      "type": "类型",
      "aliases": ["别名1", "别名2"],
      "context": "出现上下文（50字以内）"
    }
  ]
}

## 文本
{text}

只输出 JSON：
"""


class EntityExtractor:
    """Extract entities from text and store in SQLite."""

    def __init__(
        self,
        sqlite_store: "SQLiteStore",
        llm_gateway: LLMGateway | None = None,
    ):
        self.sqlite = sqlite_store
        self.llm = llm_gateway

    async def extract(self, text: str) -> list[dict[str, Any]]:
        """Extract entities from text."""
        if self.llm is not None:
            return await self._extract_with_llm(text)
        else:
            return self._fallback_extract(text)

    async def _extract_with_llm(self, text: str) -> list[dict[str, Any]]:
        """LLM-based extraction."""
        prompt = ENTITY_EXTRACTION_PROMPT.format(text=text[:3000])

        try:
            task = LLMTask(
                task_type="ner",
                messages=[LLMMessage(role="user", content=prompt)],
                temperature=0.0,
                json_mode=True,
            )
            response = await self.llm.complete(task)
            content = response.content.strip()
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            data = json.loads(content)
            return data.get("entities", [])
        except Exception as e:
            logger.warning(f"LLM entity extraction failed: {e}")
            return self._fallback_extract(text)

    def _fallback_extract(self, text: str) -> list[dict[str, Any]]:
        """Simple regex-based extraction (Chinese-aware)."""
        entities = []

        # Quoted strings (often proper nouns)
        for match in re.finditer(r"[「『""'']([^「『""'']{2,20})[」』""'']", text):
            entities.append({
                "name": match.group(1),
                "type": "other",
                "aliases": [],
                "context": text[max(0, match.start() - 20):match.end() + 20],
            })

        # Capitalized terms (English proper nouns)
        for match in re.finditer(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", text):
            entities.append({
                "name": match.group(1),
                "type": "other",
                "aliases": [],
                "context": text[max(0, match.start() - 20):match.end() + 20],
            })

        # Deduplicate by name
        seen = set()
        unique = []
        for e in entities:
            if e["name"] not in seen:
                seen.add(e["name"])
                unique.append(e)

        return unique[:20]

    async def extract_and_store(
        self,
        text: str,
        page_id: str | None = None,
        chunk_id: str | None = None,
        doc_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Extract entities and store them with mention tracking."""
        entities = await self.extract(text)

        for ent in entities:
            entity_id = self._entity_id(ent["name"])
            await self.sqlite.upsert_entity(
                entity_id=entity_id,
                name=ent["name"],
                entity_type=ent.get("type", "other"),
                aliases=ent.get("aliases", []),
            )
            if any([page_id, chunk_id, doc_id]):
                await self.sqlite.add_entity_mention(
                    entity_id=entity_id,
                    page_id=page_id,
                    chunk_id=chunk_id,
                    doc_id=doc_id,
                    context=ent.get("context", ""),
                )

        return entities

    def _entity_id(self, name: str) -> str:
        """Generate entity ID from name."""
        slug = re.sub(r"[^\w\s-]", "", name.lower())
        slug = re.sub(r"[-\s]+", "-", slug).strip("-")
        return slug[:64] or "unknown"
