"""Two layers, two jobs (ADR-031, ADR-034).

The **core layer** holds the documents other documents are read against
- contracts, rules, statutes, the company's own policies. They were
compiled into pages when they came in, and their conclusions are the
first place a question looks.

The **long tail** holds the scattered, everyday material. It is sliced
and searched, and it is searched *only* when the core layer cannot
answer: the extra latency buys noise, and noise can dilute a stable
conclusion (ADR-034).

When both layers answer and they do not agree, the core layer wins and
the answer says so - the reader has to know that the material below
says something different (AC-E-016).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)


#: How many core conclusions make the core layer "enough".
DEFAULT_MIN_CORE = 2

#: The core layer has to answer within this many seconds (AC-P-009).
CORE_BUDGET_SECONDS = 2.0

#: The layer a piece of evidence came from.
TIER_CORE = "core"
TIER_LONGTAIL = "longtail"

#: The words that carry no meaning on their own - a shared "的" is not
#: a shared subject.
_STOP = set("的了是在和与吗呢啊把被就都很也都还及或但如若因之其此并且对于关于")

_CJK = re.compile(r"[一-鿿]")
_WORD = re.compile(r"[A-Za-z0-9]+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def terms(text: str) -> set[str]:
    """The terms of a text - what two texts have in common.

    Chinese is written without spaces, so the terms are the pairs of
    characters; latin words and numbers count as terms on their own.
    The pairs that are only stopwords are dropped - they would make
    every text look alike.
    """
    lowered = (text or "").lower()
    found: set[str] = set()

    for word in _WORD.findall(lowered):
        if len(word) >= 2:
            found.add(word)

    chars = _CJK.findall(lowered)
    for index in range(len(chars) - 1):
        pair = chars[index] + chars[index + 1]
        if pair[0] in _STOP or pair[1] in _STOP:
            continue
        found.add(pair)

    if len(chars) == 1:
        # A one-character question still has to be able to match.
        found.add(chars[0])

    return found


def numbers(text: str) -> set[str]:
    """The numbers a text mentions."""
    return set(_NUMBER.findall(text or ""))


def contradicts(core_item: dict[str, Any], chunk: dict[str, Any]) -> bool:
    """Whether a slice says something different from a core conclusion.

    They have to be about the same subject, and they have to give
    different numbers - that is what a revised clause looks like when
    the old version is still lying around in the long tail.
    """
    core_text = core_item.get("text", "")
    chunk_text = chunk.get("text", "")
    if not core_text or not chunk_text:
        return False

    shared = terms(core_text) & terms(chunk_text)
    if len(shared) < 2:
        # Not about the same thing.
        return False

    core_numbers = numbers(core_text)
    chunk_numbers = numbers(chunk_text)
    if not core_numbers or not chunk_numbers:
        return False

    # The chunk repeats the core's number - it agrees with it.
    return not core_numbers.issubset(chunk_numbers)


def conflict_note(conflicts: list[dict[str, Any]]) -> str:
    """What the answer has to say about the two layers."""
    if not conflicts:
        return ""
    lines = ["核心层与长尾层的结论不一致，以核心层为准："]
    for conflict in conflicts[:3]:
        lines.append(f"- 核心层：{conflict.get('core', '')}")
        lines.append(f"- 长尾层：{conflict.get('longtail', '')}")
    return "\n".join(lines)


class TieredRetriever:
    """The evidence for a question - the core layer first."""

    def __init__(
        self,
        sqlite_store: Any,
        retriever: Any | None = None,
        min_core: int = DEFAULT_MIN_CORE,
        core_limit: int = 200,
    ):
        self.sqlite = sqlite_store
        #: The long tail is searched with this one.
        self.retriever = retriever
        self.min_core = max(1, int(min_core))
        self.core_limit = core_limit

    async def search(
        self,
        query: str,
        top_k: int = 10,
        doc_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """The evidence for a question, and what happened on the way.

        The core layer is searched first. When it has enough, the long
        tail is not searched at all - that is the short circuit
        ADR-034 asks for, and it is what keeps the answer inside two
        seconds when the core layer can answer (AC-P-009).
        """
        started = time.perf_counter()

        core = await self._core_evidence(query, top_k)
        core_ms = round((time.perf_counter() - started) * 1000, 1)

        if len(core) >= self.min_core:
            logger.info(
                f"The core layer answered with {len(core)} conclusions "
                f"in {core_ms} ms - the long tail was not searched"
            )
            return {
                "evidence": core,
                "core_found": True,
                "longtail_searched": False,
                "conflicts": [],
                "core_ms": core_ms,
            }

        longtail = await self._longtail_evidence(query, top_k, doc_ids)
        conflicts = self._conflicts(core, longtail)

        for conflict in conflicts:
            # The core layer wins - the slice is marked so the answer
            # says so, but it is not thrown away silently either.
            chunk = conflict.pop("_chunk", None)
            if chunk is not None:
                chunk["tier_conflict"] = True

        return {
            "evidence": (core + longtail)[:top_k],
            "core_found": bool(core),
            "longtail_searched": True,
            "conflicts": conflicts,
            "core_ms": core_ms,
        }

    # --------------------------------------------------------------------------
    # the core layer
    # --------------------------------------------------------------------------

    async def _core_evidence(
        self, query: str, top_k: int
    ) -> list[dict[str, Any]]:
        """The compiled conclusions that answer the question.

        The core layer is not sliced and searched - it was compiled into
        pages when it came in, and those conclusions are the evidence.
        """
        try:
            conclusions = await self.sqlite.list_active_conclusions(
                limit=self.core_limit
            )
        except Exception as e:  # noqa: BLE001 - the long tail has to answer alone
            logger.warning(f"The core layer could not be read: {e}")
            return []

        if not conclusions:
            return []

        query_terms = terms(query)
        if not query_terms:
            return []

        titles = await self._core_titles()
        scored: list[tuple[int, dict[str, Any]]] = []
        for conclusion in conclusions:
            text = conclusion.get("text", "")
            title = titles.get(conclusion.get("page_id", ""), "")
            common = query_terms & terms(f"{title} {text}")
            if not common:
                continue
            # One shared word is a coincidence; two are a subject.
            if len(common) < 2 and text not in query:
                continue
            scored.append((len(common), conclusion))

        scored.sort(key=lambda item: item[0], reverse=True)
        selected = [conclusion for _, conclusion in scored[:top_k]]

        evidence = []
        for conclusion in selected:
            item = await self._as_evidence(conclusion)
            if item is not None:
                evidence.append(item)
        return evidence

    async def _core_titles(self) -> dict[str, str]:
        """The title of every core page, by page id."""
        titles: dict[str, str] = {}
        try:
            pages = await self.sqlite.list_wiki_pages(limit=500)
        except Exception as e:  # noqa: BLE001 - the titles are a nicety
            logger.warning(f"The core pages could not be read: {e}")
            return titles
        for page in pages:
            if page.get("tier") == TIER_CORE:
                titles[page["id"]] = page.get("title", "")
        return titles

    async def _as_evidence(
        self, conclusion: dict[str, Any]
    ) -> dict[str, Any] | None:
        """A compiled conclusion, shaped like a piece of evidence."""
        anchor = conclusion.get("anchor") or {}
        if not anchor:
            # A conclusion without a position cannot back an answer.
            return None

        file_row = await self.sqlite.get_file(conclusion.get("source_doc_id") or "")
        return {
            "id": conclusion.get("id", ""),
            "doc_id": conclusion.get("source_doc_id", ""),
            "text": conclusion.get("text", ""),
            "file_name": file_row["file_name"] if file_row else "",
            "file_path": file_row["file_path"] if file_row else "",
            "anchor": anchor,
            "meta": {"anchor": anchor},
            "_score": 1.0,
            "tier": TIER_CORE,
        }

    # --------------------------------------------------------------------------
    # the long tail
    # --------------------------------------------------------------------------

    async def _longtail_evidence(
        self, query: str, top_k: int, doc_ids: list[str] | None
    ) -> list[dict[str, Any]]:
        """The scattered material - searched only when the core cannot."""
        if self.retriever is None:
            return []
        try:
            results = await self.retriever.search(
                query=query,
                top_k=top_k,
                doc_ids=doc_ids,
                exclude_doc_ids=await self._excluded_docs(),
            )
        except Exception as e:  # noqa: BLE001 - no long tail is not a failed query
            logger.warning(f"The long tail could not be searched: {e}")
            return []
        for chunk in results:
            chunk.setdefault("tier", TIER_LONGTAIL)
        return results

    async def _excluded_docs(self) -> list[str]:
        """The documents the long tail must not answer with.

        Two kinds:

        - the core layer's own documents. Their slices are not long-tail
          material: the core layer answers with its compiled pages, and
          a slice of a core document in the same answer would put the
          two layers side by side (ADR-034).
        - the documents whose compilation did not finish. Their
          content is not evidence: the page says one thing and the file
          says another, and a reader who checks one against the other
          would find a contradiction they cannot explain (AC-E-014).
        """
        excluded: list[str] = []
        try:
            excluded.extend(await self.sqlite.list_core_doc_ids())
        except Exception as e:  # noqa: BLE001 - the exclusion is a safety net
            logger.warning(f"The core documents could not be read: {e}")
        try:
            excluded.extend(await self.sqlite.list_docs_needing_recompile())
        except Exception as e:  # noqa: BLE001 - the exclusion is a safety net
            logger.warning(f"The failed documents could not be read: {e}")
        return excluded

    def _conflicts(
        self, core: list[dict[str, Any]], longtail: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Where the two layers say different things about one thing."""
        conflicts: list[dict[str, Any]] = []
        for core_item in core:
            for chunk in longtail:
                if not contradicts(core_item, chunk):
                    continue
                conflict = {
                    "core": core_item.get("text", ""),
                    "core_id": core_item.get("id", ""),
                    "longtail": str(chunk.get("text", ""))[:200],
                    "longtail_id": chunk.get("id", ""),
                    "_chunk": chunk,
                }
                conflicts.append(conflict)
        return conflicts
