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


#: The eight categories the graph knows. A category outside this list is
#: not written: the graph does not grow new kinds of thing.
ENTITY_CATEGORIES = (
    "person",     # 人物
    "project",    # 项目 / 产品
    "concept",    # 概念 / 术语
    "contract",   # 合同 / 协议
    "law",        # 法律法规
    "org",        # 组织 / 公司
    "location",   # 地点
    "other",      # 其他
)

#: What the same categories look like when the model answers in Chinese.
_CATEGORY_ALIASES = {
    "人物": "person",
    "人名": "person",
    "项目": "project",
    "产品": "project",
    "概念": "concept",
    "术语": "concept",
    "合同": "contract",
    "协议": "contract",
    "法律": "law",
    "法规": "law",
    "组织": "org",
    "公司": "org",
    "地点": "location",
    "其他": "other",
}


def normalize_category(value: Any) -> str | None:
    """The category, if it is one of the eight.

    A model that answers "员工" or "PERSON " has not named a category
    the graph knows - and the graph does not learn new ones from a
    single document.
    """
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    if key in ENTITY_CATEGORIES:
        return key
    return _CATEGORY_ALIASES.get(key)


def entity_id_for(name: str) -> str:
    """The identifier of an entity, from its name.

    One name is one entity: the same name induced from a document and
    the same name induced from an interaction have to land on the same
    row, otherwise the graph fills up with copies of the same thing.
    """
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[-\s]+", "-", slug).strip("-")
    return slug[:64] or "unknown"


def fallback_entities(text: str) -> list[dict[str, Any]]:
    """The entities in a text, found by pattern.

    This is the path taken when there is no model to ask: quoted text
    and capitalised words are the things a proper noun usually looks
    like. They are all "other" - the pattern cannot tell a person from
    a contract.
    """
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
{{
  "entities": [
    {{
      "name": "实体名",
      "type": "类型",
      "aliases": ["别名1", "别名2"],
      "context": "出现上下文（50字以内）"
    }}
  ]
}}

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
        """Extract entities from text.

        An entity whose category is outside the eight is dropped: the
        graph does not grow new kinds of thing.
        """
        if self.llm is not None:
            found = await self._extract_with_llm(text)
        else:
            found = self._fallback_extract(text)

        valid: list[dict[str, Any]] = []
        for entity in found:
            category = normalize_category(entity.get("type"))
            if category is None:
                logger.info(
                    "Dropping the entity %r: %r is not one of the eight categories",
                    entity.get("name"),
                    entity.get("type"),
                )
                continue
            entity = dict(entity)
            entity["type"] = category
            valid.append(entity)
        return valid

    async def _extract_with_llm(self, text: str) -> list[dict[str, Any]]:
        """LLM-based extraction.

        The prompt carries a JSON example, so its braces are doubled:
        `str.format` would otherwise read them as placeholders and blow
        up before the model is ever called.
        """
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
            # The prompt asks for {"entities": [...]}, but a model that
            # answers with a bare list is answering the same question.
            if isinstance(data, dict):
                return data.get("entities", [])
            if isinstance(data, list):
                return data
            return []
        except Exception as e:
            logger.warning(f"LLM entity extraction failed: {e}")
            return self._fallback_extract(text)

    def _fallback_extract(self, text: str) -> list[dict[str, Any]]:
        """Simple regex-based extraction (Chinese-aware)."""
        return fallback_entities(text)

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
        return entity_id_for(name)
