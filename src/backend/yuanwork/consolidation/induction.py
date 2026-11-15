"""The induction - what the recent interactions add up to.

The consolidation layer reads the recent episodic events and asks three
questions about them (design §1.1):

* which **entities** the interactions talk about - a thing that exists
  in the documents, so it takes effect on its own (ADR-039)
* which **rules** the interactions support - a conclusion drawn across
  events, so it waits for the user to confirm it (ADR-039)
* which **key points** belong on a page

The model is asked for JSON. A model that cannot be reached, or that
answers with something else, does not take the round down: the
entities fall back to pattern matching, and the rules and the key
points are simply not induced. A rule nobody confirmed is better than
a rule nobody checked - and a missing rule can be induced again in the
next round, a wrong one is already in the graph.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from yuanwork.wiki.entity_extractor import (
    fallback_entities,
    normalize_category,
)

logger = logging.getLogger(__name__)


#: How many recent events are read for one round of induction.
DEFAULT_WINDOW = 30

#: How much of the events is shown to the model.
MAX_EVENT_CHARS = 4000

#: The kinds of event, in the words the model is given.
_EVENT_KIND_LABELS = {
    "ask": "提问",
    "cite": "引用",
    "accept": "采纳",
    "reject": "否定",
    "add_file": "增文件",
    "remove_file": "删文件",
}


#: The judge's brief. The JSON example carries braces, so the prompt is
#: filled in with ``.replace()`` instead of ``.format()`` - which would
#: read the braces as placeholders and blow up before the model is
#: reached.
INDUCTION_PROMPT = """\
你是一个记忆归纳助手。下面是一批用户在知识库上的交互记录（情景事件）。
请从中归纳出三类东西，只输出 JSON，不要输出别的文字。

输出格式：
{
  "entities": [
    {"name": "实体名", "type": "类别", "aliases": [], "event_ids": [1]}
  ],
  "rules": [
    {"statement": "一条跨事件归纳出的规律", "event_ids": [1, 3]}
  ],
  "page_points": [
    {"topic": "页面主题", "point": "该页面应补充的要点", "event_ids": [2]}
  ]
}

要求：
1. entities 只写事件里明确出现的客观事物。type 取值只能是 person / project /
   concept / contract / law / org / location / other 之一。
2. rules 只写多条事件共同指向的结论。事件太少、看不出共性时就不要写这一类。
3. page_points 写可以补到对应主题页面上的要点，point 用一句话说清。
4. event_ids 填这条结论来自哪几条事件，用记录前面的编号。
5. 不要编造事件里没有的名字、金额与日期。哪一类没有内容就写空数组。

情景事件：
__EVENTS__

JSON：
"""


@dataclass
class InducedEntity:
    """A thing the interactions talk about."""

    name: str
    type: str
    aliases: list[str] = field(default_factory=list)
    event_ids: list[int] = field(default_factory=list)


@dataclass
class InducedRule:
    """A conclusion drawn across events - it waits for the user."""

    statement: str
    event_ids: list[int] = field(default_factory=list)


@dataclass
class InducedPagePoint:
    """A key point that belongs on a page."""

    point: str
    topic: str = ""
    page_id: str = ""
    event_ids: list[int] = field(default_factory=list)


@dataclass
class InductionResult:
    """What one round of induction found."""

    entities: list[InducedEntity] = field(default_factory=list)
    rules: list[InducedRule] = field(default_factory=list)
    page_points: list[InducedPagePoint] = field(default_factory=list)
    #: "model" | "fallback" | "empty"
    source: str = "empty"


class Inducer:
    """Reads the recent events and says what they add up to."""

    def __init__(
        self,
        llm_gateway: Any | None = None,
        window: int = DEFAULT_WINDOW,
        max_chars: int = MAX_EVENT_CHARS,
    ):
        self.llm = llm_gateway
        self.window = window
        self.max_chars = max_chars

    async def induce(self, events: list[dict[str, Any]]) -> InductionResult:
        """What the events add up to.

        Never raises: the consolidation has to finish its round even
        when the model is down.
        """
        if not events:
            return InductionResult(source="empty")

        window = list(events[: self.window])
        if self.llm is None:
            return self._fallback(window)

        try:
            return await self._induce_with_llm(window)
        except Exception as e:  # noqa: BLE001 - a missing model is not a broken round
            logger.warning(f"LLM induction failed, falling back: {e}")
            return self._fallback(window)

    # --------------------------------------------------------------------------
    # the model path
    # --------------------------------------------------------------------------

    async def _induce_with_llm(
        self, events: list[dict[str, Any]]
    ) -> InductionResult:
        from yuanwork.llm.gateway import LLMMessage, LLMTask

        prompt = INDUCTION_PROMPT.replace(
            "__EVENTS__", self._format_events(events)
        )
        task = LLMTask(
            task_type="reflection",
            messages=[LLMMessage(role="user", content=prompt)],
            temperature=0.0,
            max_tokens=1200,
            json_mode=True,
        )
        response = await self.llm.complete(task)
        return self._parse(response.content, events)

    def _format_events(self, events: list[dict[str, Any]]) -> str:
        """The window as text, numbered so the model can point back."""
        lines: list[str] = []
        used = 0
        for number, event in enumerate(events, start=1):
            kind = _EVENT_KIND_LABELS.get(event.get("kind", ""), event.get("kind", ""))
            parts = [f"[{number}] {kind}"]
            if event.get("topic"):
                parts.append(f"主题：{event['topic']}")
            if event.get("subject"):
                parts.append(f"对象：{event['subject']}")
            detail = str(event.get("detail") or "")
            if detail:
                parts.append(f"细节：{detail}")
            line = " | ".join(parts)
            if used + len(line) > self.max_chars:
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines) or "（没有记录）"

    def _parse(
        self, content: str, events: list[dict[str, Any]]
    ) -> InductionResult:
        """Read the model's answer."""
        data = self._load_json(content)
        if data is None:
            raise ValueError("the model did not answer with JSON")

        if isinstance(data, list):
            data = self._as_sections(data)

        if not isinstance(data, dict):
            raise ValueError(f"unexpected induction answer: {type(data)!r}")

        default_ids = self._event_ids(events)

        result = InductionResult(source="model")
        result.entities = [
            entity
            for entity in (
                self._read_entity(item, events, default_ids)
                for item in self._items(data.get("entities"))
            )
            if entity is not None
        ]
        result.rules = [
            rule
            for rule in (
                self._read_rule(item, events, default_ids)
                for item in self._items(data.get("rules"))
            )
            if rule is not None
        ]
        result.page_points = [
            point
            for point in (
                self._read_page_point(item, events, default_ids)
                for item in self._items(data.get("page_points"))
            )
            if point is not None
        ]
        return result

    def _load_json(self, content: str) -> Any:
        """The JSON in the answer, if there is any."""
        text = (content or "").strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _items(value: Any) -> list[Any]:
        """One section of the answer, as a list."""
        if value is None:
            return []
        if isinstance(value, list):
            return value
        return [value]

    @staticmethod
    def _as_sections(items: list[Any]) -> dict[str, Any]:
        """Read a bare list as the section its items look like.

        The prompt asks for the three sections; a model that answers
        with a single list is answering the same question, it just did
        not say which section.
        """
        if any(isinstance(i, dict) and "statement" in i for i in items):
            return {"rules": items}
        if any(isinstance(i, dict) and "point" in i for i in items):
            return {"page_points": items}
        return {"entities": items}

    # --------------------------------------------------------------------------
    # reading the three sections
    # --------------------------------------------------------------------------

    def _read_entity(
        self,
        item: Any,
        events: list[dict[str, Any]],
        default_ids: list[int],
    ) -> InducedEntity | None:
        if not isinstance(item, dict):
            return None
        name = str(item.get("name") or "").strip()
        if not name:
            return None
        category = normalize_category(item.get("type"))
        if category is None:
            # The graph has eight categories; a ninth one is a mistake,
            # and this one entity is dropped instead of the whole round.
            logger.info(
                "Dropping the induced entity %r: %r is not one of the "
                "eight categories",
                name,
                item.get("type"),
            )
            return None
        aliases = item.get("aliases")
        if not isinstance(aliases, list):
            aliases = []
        return InducedEntity(
            name=name,
            type=category,
            aliases=[str(a) for a in aliases if str(a).strip()],
            event_ids=self._read_event_ids(item, events, default_ids),
        )

    def _read_rule(
        self,
        item: Any,
        events: list[dict[str, Any]],
        default_ids: list[int],
    ) -> InducedRule | None:
        if isinstance(item, str):
            statement = item.strip()
        elif isinstance(item, dict):
            statement = str(
                item.get("statement") or item.get("content") or item.get("text") or ""
            ).strip()
        else:
            return None
        if not statement:
            return None
        return InducedRule(
            statement=statement,
            event_ids=self._read_event_ids(item, events, default_ids),
        )

    def _read_page_point(
        self,
        item: Any,
        events: list[dict[str, Any]],
        default_ids: list[int],
    ) -> InducedPagePoint | None:
        if isinstance(item, str):
            point = item.strip()
            topic = ""
        elif isinstance(item, dict):
            point = str(item.get("point") or item.get("content") or "").strip()
            topic = str(item.get("topic") or "").strip()
        else:
            return None
        if not point:
            return None
        return InducedPagePoint(
            point=point,
            topic=topic,
            event_ids=self._read_event_ids(item, events, default_ids),
        )

    def _read_event_ids(
        self,
        item: Any,
        events: list[dict[str, Any]],
        default_ids: list[int],
    ) -> list[int]:
        """Which events the conclusion came from.

        The model points at the numbers shown in the window. When it
        does not, or points at something that is not there, the whole
        window is the trace - the conclusion really was drawn from all
        of it.
        """
        numbers: Any = None
        if isinstance(item, dict):
            numbers = item.get("event_ids")
        if not isinstance(numbers, list) or not numbers:
            return list(default_ids)

        picked: list[int] = []
        for number in numbers:
            try:
                index = int(number)
            except (TypeError, ValueError):
                continue
            if 1 <= index <= len(events):
                event_id = events[index - 1].get("id")
                if event_id is not None and event_id not in picked:
                    picked.append(event_id)
        return picked or list(default_ids)

    @staticmethod
    def _event_ids(events: list[dict[str, Any]]) -> list[int]:
        """The identifiers of every event in the window."""
        ids: list[int] = []
        for event in events:
            event_id = event.get("id")
            if event_id is not None and event_id not in ids:
                ids.append(event_id)
        return ids

    # --------------------------------------------------------------------------
    # the fallback
    # --------------------------------------------------------------------------

    def _fallback(self, events: list[dict[str, Any]]) -> InductionResult:
        """Without a model only the entities are induced.

        A rule needs a model to draw it - pattern matching would make
        rules out of coincidence, and a wrong rule that is waiting for
        confirmation is still a wrong rule the user has to read.
        """
        text = "\n".join(
            str(event.get("detail") or "")
            for event in events
            if event.get("detail")
        )
        entities = [
            InducedEntity(
                name=found["name"],
                type=normalize_category(found.get("type")) or "other",
                aliases=list(found.get("aliases") or []),
                event_ids=self._event_ids(events),
            )
            for found in fallback_entities(text)
        ]
        return InductionResult(entities=entities, source="fallback")
