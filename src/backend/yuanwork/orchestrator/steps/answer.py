"""Step 4: Answer Generation - synthesize final answer with citations."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from jinja2 import Environment, FileSystemLoader
from pathlib import Path

from yuanwork.llm.gateway import LLMGateway, LLMMessage, LLMTask
from yuanwork.privacy.redactor import Redactor
from yuanwork.utils.chunk_meta import chunk_location, chunk_meta

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts"
_jinja_env = Environment(loader=FileSystemLoader(str(_PROMPTS_DIR)), autoescape=False)


#: The citation markers the answer prompt asks for: [1], [2][3], or
#: [1,2]. Anything else in brackets is not a citation.
_CITATION_PATTERN = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


def extract_terms(answer: str, vocabulary: Any) -> list[str]:
    """The terms of the answer that the knowledge base knows by name.

    A word the knowledge base does not know has no context to show -
    offering it would only send the user to an empty page. The
    vocabulary is the knowledge base's own names: the entities and the
    page titles.
    """
    if not answer:
        return []

    found: list[str] = []
    # The longest first: "履约保证金" before "保证金".
    for term in sorted({str(v) for v in vocabulary or [] if v}, key=len, reverse=True):
        if len(term) < 2:
            continue
        if term not in answer:
            continue
        if any(term in kept for kept in found):
            # A part of a term that was already found - offering both
            # would point at the same word twice.
            continue
        found.append(term)
    return found


@dataclass
class CitationValidation:
    """What the answer cites, and whether it adds up.

    ``cited`` holds the 1-based numbers of the evidence the answer
    used. ``reason`` says why the check failed - ``no_citations`` when
    the answer cites nothing at all, ``citation_out_of_range`` when a
    number points at evidence that does not exist.
    """

    ok: bool
    cited: list[int] = field(default_factory=list)
    reason: str | None = None


def validate_citations(answer: str, evidence_count: int) -> CitationValidation:
    """Check the citation numbers in the answer against the evidence.

    The model is asked to mark every fact with the number of the piece
    of evidence it came from. A number that points at nothing is a
    broken citation, and a broken citation means the user cannot check
    the claim it is attached to - so the answer does not get to stand
    as a conclusion (ADR-009).
    """
    numbers: list[int] = []
    for match in _CITATION_PATTERN.finditer(answer or ""):
        for part in match.group(1).split(","):
            part = part.strip()
            if part.isdigit():
                numbers.append(int(part))

    if not numbers:
        # An answer that cites nothing is an answer the user cannot
        # check - and it is also how the model says "the evidence does
        # not answer this", which is a refusal in disguise.
        return CitationValidation(ok=False, reason="no_citations")

    if any(number < 1 or number > evidence_count for number in numbers):
        return CitationValidation(ok=False, reason="citation_out_of_range")

    return CitationValidation(ok=True, cited=sorted(set(numbers)))


def _build_answer_task(
    query: str,
    evidence: list[dict[str, Any]],
    confidence: dict[str, Any],
    redactor: "Redactor | None" = None,
    tier_conflict: str = "",
    follow_up: str = "",
) -> LLMTask:
    """Render the answer prompt into an LLM task.

    Shared by the blocking and the streaming variant so both send the
    exact same prompt to the model.

    The evidence is redacted *here* - on its way to the model - and not
    in the state it came from: the citation the user reads is their own
    data and stays as it is, while the model never sees a phone number
    or an id card.

    ``tier_conflict`` says that the core layer and the long tail gave
    different answers. The core layer wins (ADR-034), and the answer
    has to tell the reader that the two disagree (AC-E-016).

    ``follow_up`` is the previous turn's positions and the reader's own
    question - **never the previous answer** (ADR-057). It tells the
    model what this question is about; it is not evidence.
    """
    template = _jinja_env.get_template("answer.j2")

    # Format evidence for prompt
    formatted_evidence = []
    for chunk in evidence:
        # The evidence comes from the retriever, where the location fields
        # are flat columns. The prompt demands "第 N 页" / "Slide N"
        # citations, so the page number has to actually reach the model -
        # reading the nested "meta" dict alone would silently drop it.
        meta = chunk_meta(chunk)
        text = chunk.get("text", "")[:2000]  # Truncate very long chunks
        if redactor is not None:
            text = redactor.redact(text)
        formatted_evidence.append({
            "file_name": chunk.get("file_name", "unknown"),
            "meta": meta,
            "text": text,
        })

    prompt = template.render(
        query=query,
        evidence=formatted_evidence,
        confidence=confidence,
        tier_conflict=tier_conflict,
        follow_up=follow_up,
    )

    return LLMTask(
        task_type="answer",
        messages=[
            LLMMessage(role="system", content=prompt),
            LLMMessage(role="user", content=query),
        ],
        temperature=0.1,
        max_tokens=2000,
    )


async def generate_answer(
    llm: LLMGateway,
    query: str,
    evidence: list[dict[str, Any]],
    confidence: dict[str, Any],
    redactor: Redactor | None = None,
    tier_conflict: str = "",
    follow_up: str = "",
) -> str:
    """Generate final answer with mandatory citations.

    The prompt enforces:
    - Citation [n] format
    - No fabrication
    - Refusal when evidence insufficient

    The evidence is redacted before it goes into the prompt.
    """
    task = _build_answer_task(
        query,
        evidence,
        confidence,
        redactor=redactor,
        tier_conflict=tier_conflict,
        follow_up=follow_up,
    )
    response = await llm.complete(task)
    return response.content


async def generate_answer_stream(
    llm: LLMGateway,
    query: str,
    evidence: list[dict[str, Any]],
    confidence: dict[str, Any],
    redactor: Redactor | None = None,
    tier_conflict: str = "",
    follow_up: str = "",
) -> AsyncIterator[str]:
    """Generate the answer, yielding text deltas as the model produces them.

    Same prompt as generate_answer(); the difference is that the caller
    receives the text incrementally instead of waiting for the whole answer.
    """
    task = _build_answer_task(
        query,
        evidence,
        confidence,
        redactor=redactor,
        tier_conflict=tier_conflict,
        follow_up=follow_up,
    )
    async for delta in llm.stream(task):
        yield delta


async def generate_refusal(
    candidates: list[dict[str, Any]],
    kb_empty: bool = False,
    evidence: list[dict[str, Any]] | None = None,
    reason: str | None = None,
) -> str:
    """Generate a refusal message listing most likely locations.

    ``kb_empty`` says the knowledge base holds nothing searchable at all.
    That is a different situation from "the question is not in the
    documents", and the fix is different too - so it gets its own wording.

    ``evidence`` is what was actually retrieved: it knows where each
    piece came from, so the refusal can point at a page or a section
    instead of only at a file name. ``reason`` is the gate's verdict -
    the three reasons need three different suggestions, because the
    three problems need three different fixes.
    """
    if kb_empty:
        template = _jinja_env.get_template("refusal_empty.j2")
        return template.render()

    template = _jinja_env.get_template("refusal.j2")

    return template.render(
        opening=_refusal_opening(reason),
        reason=reason,
        positions=refusal_positions(evidence, candidates),
    )


#: What the user reads for each reason the gate gave. The refusal must
#: not sound like a verdict on their question when it is really a
#: statement about the documents.
REFUSAL_OPENINGS = {
    "insufficient_evidence": (
        "我在你的知识库里没有找到足够明确的证据来回答这个问题。"
    ),
    "unlocatable_evidence": (
        "我找到了相关的资料，但它们没有可定位的位置——没法让你逐条核对，"
        "所以我先不回答。"
    ),
    "model_insufficient": (
        "我找到了相关的资料，但它们没有回答你问的这件事，所以我先不回答。"
    ),
    "citation_invalid": (
        "答案里的引用和找到的资料对不上，我不能把它当成结论给你。"
    ),
    "index_missing": (
        "你问的这份文件在账本里，可索引里没有它——"
        "所以我读不到它，也就没法回答。"
    ),
}


def _refusal_opening(reason: str | None) -> str:
    """The first line, which says what actually went wrong."""
    return REFUSAL_OPENINGS.get(
        reason or "",
        REFUSAL_OPENINGS["insufficient_evidence"],
    )


def refusal_positions(
    evidence: list[dict[str, Any]] | None,
    candidates: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    """Where the answer most likely is.

    The evidence comes first - it knows the page or the section it was
    cut from, which is what the user needs to check it. The candidate
    files are the fallback: they are what we know exists.

    Every position carries the path as well: the reader is told to
    "click the position below and look at the original", so the
    positions have to be clickable (TASK-085).
    """
    positions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for chunk in evidence or []:
        file_name = chunk.get("file_name", "")
        if not file_name:
            continue
        location = chunk_location(chunk)
        key = (file_name, location)
        if key in seen:
            continue
        seen.add(key)
        positions.append({
            "file_name": file_name,
            "location": location,
            "file_path": str(chunk.get("file_path") or ""),
        })

    for candidate in candidates or []:
        file_name = candidate.get("file_name", "")
        if not file_name:
            continue
        key = (file_name, "")
        if key in seen:
            continue
        seen.add(key)
        positions.append({
            "file_name": file_name,
            "location": "",
            "file_path": str(candidate.get("file_path") or ""),
        })

    return positions[:5]
