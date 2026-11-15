"""AgentLoop - 3-step reasoning state machine.

Pipeline:
  Step 1 (Understand): Parse user query into structured intent
  Step 2 (Localize):  Identify candidate files
  Step 3 (Extract):    Get relevant chunks within candidates
  Step 4 (Answer):    Synthesize answer with citations, or refuse
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

from yuanwork.api.errors import classify_exception
from yuanwork.api.schemas import ChatMessage
from yuanwork.llm.gateway import LLMGateway
from yuanwork.memory.episodic import EpisodicMemory
from yuanwork.memory.episodic_recorder import EpisodicEvent
from yuanwork.memory.semantic import SemanticMemory
from yuanwork.orchestrator import cancel
from yuanwork.orchestrator.steps.understand import understand_query
from yuanwork.orchestrator.steps.localize import localize_files
from yuanwork.orchestrator.steps.extract import extract_evidence
from yuanwork.orchestrator.steps.answer import (
    CitationValidation,
    generate_answer,
    generate_answer_stream,
    generate_refusal,
    validate_citations,
)
from yuanwork.retrieval.confidence import ConfidenceScorer
from yuanwork.retrieval.evidence_gate import (
    REASON_GATE_ERROR,
    REASON_INSUFFICIENT,
    REASON_MODEL_UNAVAILABLE,
    EvidenceGate,
    EvidenceGateResult,
)
from yuanwork.retrieval.hybrid import HybridRetriever
from yuanwork.retrieval.model_gate import ModelSelfAssessGate
from yuanwork.retrieval.tiered import TieredRetriever, conflict_note
from yuanwork.utils.chunk_meta import chunk_location, chunk_meta

if TYPE_CHECKING:
    from yuanwork.consolidation.engine import ConsolidationEngine
    from yuanwork.db.sqlite import SQLiteStore
    from yuanwork.memory.episodic_recorder import EpisodicRecorder
    from yuanwork.privacy.redactor import Redactor
    from yuanwork.wiki.trigger import WikiCompileTrigger

logger = logging.getLogger(__name__)

#: What the user reads when the model could not be reached. It must not
#: sound like "your question has no answer" - that is a different thing.
MODEL_UNAVAILABLE_MESSAGE = (
    "抱歉，模型暂时不可用，我没有能给出答案。请稍后重试。"
)


@dataclass
class AgentState:
    """State carried through the agent loop."""

    query: str
    session_id: str
    history: list[ChatMessage] = field(default_factory=list)

    # Step outputs
    understanding: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    confidence: dict[str, Any] | None = None
    # The verdict of the evidence gate - may this evidence back an answer?
    gate: EvidenceGateResult | None = None
    # Where the core layer and the long tail said different things
    # (AC-E-016). The core layer wins; the answer has to say so.
    tier_conflicts: list[dict[str, Any]] = field(default_factory=list)

    # Final outputs
    answer: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None
    #: The user stopped the run - the answer is what came before.
    cancelled: bool = False

    # Memory bookkeeping
    user_episode_id: int | None = None
    assistant_episode_id: int | None = None

    # Wiki compile outcome (Stage 2)
    wiki_compile: dict[str, Any] | None = None

    # Debug
    trace: list[dict[str, Any]] = field(default_factory=list)


class AgentLoop:
    """3-step reasoning state machine."""

    def __init__(
        self,
        llm_gateway: LLMGateway,
        semantic_memory: SemanticMemory,
        episodic_memory: EpisodicMemory,
        consolidation_engine: "ConsolidationEngine | None" = None,
        wiki_compile_trigger: "WikiCompileTrigger | None" = None,
        redactor: "Redactor | None" = None,
        episodic_recorder: "EpisodicRecorder | None" = None,
        sqlite_store: "SQLiteStore | None" = None,
    ):
        self.llm = llm_gateway
        self.semantic = semantic_memory
        self.episodic = episodic_memory
        self.consolidation = consolidation_engine
        self.wiki_trigger = wiki_compile_trigger
        # The recorder keeps the interactions - asking, citing,
        # accepting, rejecting, adding and removing files. They are what
        # the memory engineering layer reads.
        self.recorder = episodic_recorder
        # The redactor masks sensitive fields on their way to the model.
        # Without one, the evidence goes out as it is.
        self.redactor = redactor
        self.retriever = HybridRetriever(semantic_memory)
        # The store holds the knowledge base's own names - the vocabulary
        # the answer's terms are looked up in (ADR-041).
        self.sqlite = sqlite_store
        # The core layer is searched first, and the long tail only when
        # it cannot answer (ADR-034). Without a store there is no core
        # layer to search, and the long tail answers on its own.
        self.tiered = (
            TieredRetriever(sqlite_store=sqlite_store, retriever=self.retriever)
            if sqlite_store is not None
            else None
        )
        self.scorer = ConfidenceScorer()
        # The gate decides whether the evidence may back an answer. The
        # confidence score is only a signal the user gets to see.
        #
        # Both halves of it: the hard rule (enough pieces, every piece
        # locatable) and the model's own judgement of whether the
        # evidence answers the question. The more conservative of the
        # two wins (ADR-008).
        self.gate = EvidenceGate(
            model_gate=ModelSelfAssessGate(llm_gateway, redactor=redactor)
        )

    async def run(
        self,
        query: str,
        session_id: str,
        history: list[ChatMessage] | None = None,
        opts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the agent loop and return the final state as a dict.

        The run registers itself, so the cancel endpoint can reach it -
        the blocking answer call is cancelled from the outside.
        """
        state = AgentState(
            query=query,
            session_id=session_id,
            history=history or [],
        )
        cancel_event = cancel.start(session_id)
        try:
            await self._step_understand(state)
            await self._step_localize(state)
            await self._step_extract(state)
            # Stage 2: opportunistic Wiki page compile (fire-and-forget, after extract)
            await self._step_maybe_compile_wiki_page(state)
            # The evidence gate: may this evidence back an answer at all?
            await self._step_gate(state)
            if cancel_event.is_set():
                state.cancelled = True
            else:
                await self._step_answer(state, cancel_event)
            if not state.cancelled:
                await self._step_check_conflicts(state)
        except cancel.Cancelled:
            # The user stopped it - the answer is what came before.
            state.cancelled = True
        finally:
            cancel.finish(session_id)

        # Record episodic memory for this turn (best-effort, don't fail chat)
        await self._record_episode_safe(state)

        # Maybe this turn ends a consolidation round (ADR-015).
        self._schedule_consolidation()

        return {
            "answer": state.answer,
            "citations": state.citations,
            "confidence": state.confidence["confidence"] if state.confidence else 0.0,
            "refused": state.refused,
            "refusal_reason": state.refusal_reason,
            "cancelled": state.cancelled,
            "conflicts": state.conflicts,
            "wiki_compile": state.wiki_compile,
            # The terms the reader may not know - each one can be
            # opened up to the text it stands for (ADR-041).
            "terms": await self._answer_terms(state.answer),
            "trace": state.trace,
        }

    async def stream(
        self,
        query: str,
        session_id: str,
        history: list[ChatMessage] | None = None,
        opts: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream events during agent execution.

        The run registers itself, so the cancel endpoint can reach it.
        When the user cancels, the run stops between the steps and
        between the tokens - the backend stops pulling from the model.
        """
        state = AgentState(
            query=query,
            session_id=session_id,
            history=history or [],
        )
        cancel_event = cancel.start(session_id)
        try:
            async for event in self._stream_steps(state, cancel_event):
                yield event
        finally:
            cancel.finish(session_id)

    async def _stream_steps(
        self, state: AgentState, cancel_event: asyncio.Event
    ) -> AsyncIterator[dict[str, Any]]:
        """The steps, and the events they produce."""
        session_id = state.session_id

        # Step 1
        yield {"type": "trace", "data": {"step": "understand", "status": "started"}}
        await self._step_understand(state)
        yield {
            "type": "trace",
            "data": {
                "step": "understand",
                "status": "completed",
                "understanding": state.understanding,
            },
        }

        # Step 2
        yield {"type": "trace", "data": {"step": "localize", "status": "started"}}
        await self._step_localize(state)
        yield {
            "type": "trace",
            "data": {
                "step": "localize",
                "status": "completed",
                "candidate_count": len(state.candidates),
                "candidates": [
                    {"file_name": c["file_name"], "score": c["score"]}
                    for c in state.candidates
                ],
            },
        }

        # Step 3
        yield {"type": "trace", "data": {"step": "extract", "status": "started"}}
        await self._step_extract(state)
        yield {
            "type": "trace",
            "data": {
                "step": "extract",
                "status": "completed",
                "evidence_count": len(state.evidence),
            },
        }

        # Step 3.5 (Stage 2): opportunistic Wiki page compile from extract output
        yield {"type": "trace", "data": {"step": "wiki_compile", "status": "started"}}
        await self._step_maybe_compile_wiki_page(state)
        yield {
            "type": "trace",
            "data": {
                "step": "wiki_compile",
                "status": "completed",
                "wiki_compile": state.wiki_compile,
            },
        }

        # The evidence gate
        yield {"type": "trace", "data": {"step": "gate", "status": "started"}}
        await self._step_gate(state)
        yield {
            "type": "trace",
            "data": {
                "step": "gate",
                "status": "completed",
                "gate": state.gate.to_dict() if state.gate else None,
            },
        }

        # Step 4 - streamed: the answer arrives token by token.
        yield {"type": "trace", "data": {"step": "answer", "status": "started"}}
        async for delta in self._generate_answer_tokens(state, cancel_event):
            yield {"type": "token", "data": {"text": delta}}
            if cancel_event.is_set():
                # The user stopped it - what came before is what there is.
                state.cancelled = True
                logger.info(f"The run of session {session_id} was cancelled")
                break
        yield {
            "type": "trace",
            "data": {
                "step": "answer",
                "status": "cancelled" if state.cancelled else "completed",
                "confidence": state.confidence,
                "refused": state.refused,
            },
        }

        # Step 5 (Stage 2): LLM-vs-KB conflict detection
        yield {"type": "trace", "data": {"step": "check_conflicts", "status": "started"}}
        await self._step_check_conflicts(state)
        yield {
            "type": "trace",
            "data": {
                "step": "check_conflicts",
                "status": "completed",
                "conflict_count": len(state.conflicts),
            },
        }

        # Record episodic memory
        await self._record_episode_safe(state)

        # Maybe this turn ends a consolidation round (ADR-015).
        self._schedule_consolidation()

        # Final events
        if state.refused:
            yield {
                "type": "refusal",
                "data": {
                    "reason": state.refusal_reason,
                    "message": state.answer,
                    "candidates": state.candidates,
                },
            }
        else:
            yield {
                "type": "answer",
                "data": {
                    "text": state.answer,
                },
            }
            if state.conflicts:
                yield {
                    "type": "conflict",
                    "data": {"conflicts": state.conflicts},
                }

        # Citations are emitted in both branches: a half-generated answer
        # (generation error) still points at real evidence, and dropping
        # them would leave the user with text they cannot check.
        yield {
            "type": "citations",
            "data": {"citations": state.citations},
        }

        yield {
            "type": "done",
            "data": {
                "session_id": session_id,
                "response": {
                    "session_id": session_id,
                    "answer": state.answer,
                    "citations": state.citations,
                    "confidence": (
                        state.confidence["confidence"] if state.confidence else 0.0
                    ),
                    "refused": state.refused,
                    "refusal_reason": state.refusal_reason,
                    "cancelled": state.cancelled,
                    "conflicts": state.conflicts,
                    "wiki_compile": state.wiki_compile,
                    "terms": await self._answer_terms(state.answer),
                    "trace": state.trace,
                },
            },
        }

    # ===== Step implementations =====

    async def _step_understand(self, state: AgentState) -> None:
        """Step 1: Understand the query."""
        try:
            state.understanding = await understand_query(self.llm, state.query)
        except Exception as e:
            logger.exception(f"Understand step failed: {e}")
            state.understanding = {
                "intent": "general",
                "topic": state.query[:50],
                "file_hint": None,
                "time_hint": None,
                "entity_hints": [],
                "requires_evidence": True,
                "needs_refusal_if_no_match": True,
            }

        state.trace.append({
            "step": "understand",
            "output": state.understanding,
        })

        await self._record_ask(state)

    async def _record_ask(self, state: AgentState) -> None:
        """The question is an interaction - it is recorded as one.

        The recorder never raises: a question that could not be written
        down still gets answered.
        """
        if self.recorder is None:
            return

        understanding = state.understanding or {}
        try:
            await self.recorder.record(
                EpisodicEvent(
                    kind="ask",
                    session_id=state.session_id,
                    subject=str(understanding.get("file_hint") or ""),
                    detail=(state.query or "")[:500],
                    topic=str(understanding.get("topic") or ""),
                    importance=0.5,
                )
            )
        except Exception as e:  # noqa: BLE001 - a lost event is not a broken chat
            logger.warning(f"Could not record the question as an event: {e}")

    async def _step_localize(self, state: AgentState) -> None:
        """Step 2: Identify candidate files."""
        from yuanwork.api.deps import get_sqlite_store

        sqlite = get_sqlite_store()

        state.candidates = await localize_files(
            query=state.query,
            understanding=state.understanding or {},
            sqlite=sqlite,
            semantic=self.semantic,
            top_k_files=5,
        )

        state.trace.append({
            "step": "localize",
            "candidate_count": len(state.candidates),
        })

    async def _step_extract(self, state: AgentState) -> None:
        """Step 3: Extract evidence chunks."""
        result = await extract_evidence(
            query=state.query,
            candidates=state.candidates,
            retriever=self.retriever,
            top_k=10,
            tiered=self.tiered,
        )
        state.evidence = result["evidence"]
        state.tier_conflicts = result.get("conflicts") or []

        # Compute confidence
        state.confidence = self.scorer.score(state.evidence)

        state.trace.append({
            "step": "extract",
            "evidence_count": len(state.evidence),
            "core_found": result.get("core_found", False),
            "longtail_searched": result.get("longtail_searched", True),
            "tier_conflicts": len(state.tier_conflicts),
            "confidence": state.confidence,
        })

    async def _step_maybe_compile_wiki_page(self, state: AgentState) -> None:
        """Step 3.5 (Stage 2): opportunistically compile a Wiki page from
        the extracted evidence.

        Fires only when:
          - Wiki trigger is configured
          - Trigger conditions are met (multi-doc, enough evidence, etc.)
          - Throttle allows

        Errors are swallowed: a failed compile must never fail the chat.
        """
        if self.wiki_trigger is None:
            return
        try:
            result = await self.wiki_trigger.maybe_compile(
                evidence=state.evidence,
                understanding=state.understanding,
            )
        except Exception as e:
            logger.warning(f"Wiki compile step crashed: {e}")
            result = {"triggered": False, "reason": "exception", "error": classify_exception(e),
                      "page_id": None, "title": None}

        state.wiki_compile = result
        state.trace.append({"step": "wiki_compile", **result})

    async def _step_gate(self, state: AgentState) -> None:
        """The evidence gate: may this evidence back an answer?

        The gate is a step of its own - it is the decision point between
        "we found something" and "we may answer". A gate that cannot
        decide refuses: the conservative side is the one that keeps the
        promise of never making a claim the documents do not support.

        The time it took is recorded: the refusal decision and the
        conflict decision share a budget (AC-P-004), and a budget that
        is not measured is a budget that is not kept.
        """
        started = time.perf_counter()
        try:
            state.gate = await self.gate.evaluate(state.evidence, state.query)
        except Exception as e:  # noqa: BLE001 - a broken gate must not answer
            logger.exception(f"Evidence gate failed: {e}")
            state.gate = EvidenceGateResult(
                passed=False,
                hard_rule_ok=False,
                model_ok=False,
                model_evaluated=False,
                reason=REASON_GATE_ERROR,
                sufficient_count=len(state.evidence),
            )
        duration_ms = (time.perf_counter() - started) * 1000

        state.trace.append({
            "step": "gate",
            "gate": state.gate.to_dict(),
            "duration_ms": round(duration_ms, 1),
        })

    async def _step_answer(
        self, state: AgentState, cancel_event: asyncio.Event | None = None
    ) -> None:
        """Step 4: Generate answer or refuse (blocking variant)."""
        # The gate decides - the confidence score does not.
        if state.gate is not None and not state.gate.passed:
            await self._apply_refusal(state)
            return

        # Generate answer
        try:
            if cancel_event is None:
                state.answer = await generate_answer(
                    llm=self.llm,
                    query=state.query,
                    evidence=state.evidence,
                    confidence=state.confidence or {},
                    redactor=self.redactor,
                    tier_conflict=conflict_note(state.tier_conflicts),
                )
            else:
                # The call cannot be interrupted from the inside, so it
                # runs as a task and is cancelled from the outside.
                state.answer = await cancel.run_until_cancelled(
                    generate_answer(
                        llm=self.llm,
                        query=state.query,
                        evidence=state.evidence,
                        confidence=state.confidence or {},
                        redactor=self.redactor,
                        tier_conflict=conflict_note(state.tier_conflicts),
                    ),
                    cancel_event,
                )
            # An empty completion is not an answer - see the streaming
            # path for the same check.
            if not (state.answer or "").strip():
                raise RuntimeError("模型返回了空回答")
            state.refused = False
        except cancel.Cancelled:
            # The user stopped it - that is not a model failure.
            raise
        except Exception as e:
            logger.exception(f"Answer generation failed: {e}")
            state.refused = True
            state.refusal_reason = "model_unavailable"
            state.answer = MODEL_UNAVAILABLE_MESSAGE
            state.trace.append({"step": "answer", "error": classify_exception(e)})
            # Keep the citations, same as the streaming path.
            state.citations = self._build_citations(state.evidence)
            return

        # Check the citations before the answer counts as an answer.
        validation = validate_citations(state.answer, len(state.evidence))
        if not validation.ok:
            await self._apply_citation_refusal(state, validation)
            return

        # Only the evidence the answer actually cited is a citation.
        state.citations = self._build_citations(
            state.evidence, only=set(validation.cited)
        )

        state.trace.append({
            "step": "answer",
            "refused": False,
            "citation_count": len(state.citations),
        })

    async def _apply_citation_refusal(
        self,
        state: AgentState,
        validation: CitationValidation,
    ) -> None:
        """The answer's citations do not add up - it does not get to
        stand as an answer.

        The evidence itself is real, so the citations stay: the user
        can go and look. What does not stay is the model's claim that
        this is what the documents say.
        """
        state.refused = True
        state.refusal_reason = "citation_invalid"
        state.answer = await generate_refusal(
            state.candidates,
            kb_empty=await self._knowledge_base_is_empty(),
            evidence=state.evidence,
            reason="citation_invalid",
        )
        state.citations = self._build_citations(state.evidence)
        state.trace.append({
            "step": "answer",
            "refused": True,
            "reason": state.refusal_reason,
            "citation_check": validation.reason,
        })

    async def _apply_refusal(self, state: AgentState) -> None:
        """Fill the state with a refusal message (no answer is generated)."""
        state.refused = True
        # The reason comes from the gate: it says *which* gate said no,
        # and the refusal text can then point at the right fix.
        state.refusal_reason = (
            state.gate.reason if state.gate is not None else REASON_INSUFFICIENT
        )

        if state.refusal_reason == REASON_MODEL_UNAVAILABLE:
            # The model could not be reached. Telling the user that the
            # documents were silent would be a different - and wrong -
            # thing to say (design 4.3).
            state.answer = MODEL_UNAVAILABLE_MESSAGE
            # The evidence is real and the user may want to look at it -
            # the same reason a half-generated answer keeps its citations.
            state.citations = self._build_citations(state.evidence)
        else:
            state.answer = await generate_refusal(
                state.candidates,
                kb_empty=await self._knowledge_base_is_empty(),
                evidence=state.evidence,
                reason=state.refusal_reason,
            )
            state.citations = []
        state.trace.append({
            "step": "answer",
            "refused": True,
            "reason": state.refusal_reason,
        })

    async def _knowledge_base_is_empty(self) -> bool:
        """Whether the knowledge base holds nothing searchable at all.

        A store that cannot answer the question is treated as "not empty":
        guessing would tell the user to add files it already has.
        """
        counter = getattr(self.semantic, "count", None)
        if counter is None:
            return False
        try:
            return await counter() == 0
        except Exception:  # noqa: BLE001 - the count is a nicety, not a requirement
            logger.exception("Cannot count the knowledge base")
            return False

    async def _generate_answer_tokens(
        self, state: AgentState, cancel_event: asyncio.Event | None = None
    ) -> AsyncIterator[str]:
        """Step 4 (streaming): generate the answer, yielding deltas as they arrive.

        On the refusal path nothing is yielded. On the normal path every LLM
        delta is yielded and appended to state.answer, so the caller can
        forward it to the client immediately.

        If the model fails part-way through, whatever was already generated
        is kept and an error notice is appended, so the user does not lose
        the text that was produced.

        When the run is cancelled the model is not asked for another
        token: the answer stops where it is.
        """
        if state.gate is not None and not state.gate.passed:
            await self._apply_refusal(state)
            return

        try:
            async for delta in generate_answer_stream(
                llm=self.llm,
                query=state.query,
                evidence=state.evidence,
                confidence=state.confidence or {},
                redactor=self.redactor,
                tier_conflict=conflict_note(state.tier_conflicts),
            ):
                if cancel_event is not None and cancel_event.is_set():
                    # Stop asking the model - the user does not want the
                    # rest of it.
                    return
                state.answer += delta
                yield delta
            # An empty completion is not an answer. Reporting it as a
            # success would hand the user a blank bubble with citations
            # attached and no explanation of what went wrong.
            if not state.answer.strip():
                raise RuntimeError("模型返回了空回答")
            state.refused = False
        except Exception as e:
            logger.exception(f"Answer generation failed: {e}")
            state.refused = True
            state.refusal_reason = "model_unavailable"
            notice = MODEL_UNAVAILABLE_MESSAGE
            state.answer = f"{state.answer}\n\n{notice}" if state.answer else notice
            state.trace.append({"step": "answer", "error": classify_exception(e)})
            # Keep the citations: a half-generated answer still points at
            # real evidence, and dropping them would leave the user with
            # text they cannot check.
            state.citations = self._build_citations(state.evidence)
            return

        # Check the citations before the answer counts as an answer.
        validation = validate_citations(state.answer, len(state.evidence))
        if not validation.ok:
            # The text has already been streamed, so it cannot be taken
            # back. The refusal message replaces it on the client, and
            # the citations stay - the evidence is real even when the
            # model's numbering is not.
            await self._apply_citation_refusal(state, validation)
            return

        # Build citations from evidence
        state.citations = self._build_citations(
            state.evidence, only=set(validation.cited)
        )

        state.trace.append({
            "step": "answer",
            "refused": False,
            "citation_count": len(state.citations),
        })

    def _build_citations(
        self,
        evidence: list[dict[str, Any]],
        only: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Convert evidence chunks into citation objects.

        The evidence comes from the retriever, so its location fields are
        flat columns (``page``, ``slide_number``, ``section``) rather
        than the nested ``meta`` dict the chunker produces - `chunk_meta`
        reads whichever shape it is given.

        ``only`` keeps just the pieces the answer actually cited. The
        number a citation carries is the number the piece had in the
        evidence list, so it still matches the ``[n]`` in the answer.
        """
        citations = []
        for idx, chunk in enumerate(evidence, start=1):
            if only is not None and idx not in only:
                continue
            meta = chunk_meta(chunk)
            citation = {
                "index": idx,
                "file_name": chunk.get("file_name", ""),
                "file_path": chunk.get("file_path", ""),
                "page": meta.get("page"),
                "slide_number": meta.get("slide_number"),
                "section": meta.get("heading") or meta.get("section"),
                "sheet": meta.get("sheet"),
                "row": meta.get("row"),
                "anchor": meta.get("anchor"),
                "location": chunk_location(chunk),
                "text": chunk.get("text", "")[:500],  # Truncate for display
                "chunk_id": chunk.get("id", ""),
            }
            citations.append(citation)
        return citations

    async def _answer_terms(self, answer: str) -> list[str]:
        """The terms in the answer that the knowledge base knows.

        A term the knowledge base does not know has no context to show,
        so it is not offered as one. The vocabulary is the knowledge
        base's own names - the entities and the page titles (ADR-041).
        """
        if not answer or self.sqlite is None:
            return []

        from yuanwork.orchestrator.steps.answer import extract_terms

        try:
            entities = await self.sqlite.list_entities(status="active", limit=500)
            pages = await self.sqlite.list_wiki_pages(limit=500)
        except Exception as e:  # noqa: BLE001 - the terms are a nicety
            logger.warning(f"Could not read the vocabulary: {e}")
            return []

        vocabulary = [e.get("name", "") for e in entities]
        vocabulary += [p.get("title", "") for p in pages]
        return extract_terms(answer, vocabulary)

    async def _step_check_conflicts(self, state: AgentState) -> None:
        """Step 5 (Stage 2): detect LLM-vs-KB conflicts.

        Runs only if we generated an answer (not refused) and have evidence.
        Persists detected conflicts to SQLite via the consolidation engine.

        Heuristics (any one of these flags a conflict):
        1. `detect_conflict()` LLM call returns verdict=conflict with claims.
        2. The answer text itself contains conflict markers (e.g. "OUTDATED",
           "过时", "已弃用", "⚠️") — the answer prompt template instructs the
           LLM to add such a warning, so its presence is itself a signal.
        """
        # Skip if refused or no usable answer/evidence
        if state.refused or not state.answer or not state.evidence:
            return

        from yuanwork.consolidation.conflict import (
            ConflictJudge,
            detect_conflict,
            format_conflict_message,
            rewrite_with_knowledge_base,
        )

        # The model's answer, before anything is done to it.
        model_answer = state.answer or ""

        # The pre-check: not every answer has to be checked against the
        # documents. An answer that says nothing specific cannot
        # contradict them in a way the user would act on - and the
        # check costs a model call (AC-F-029, AC-P-004). The reason for
        # skipping is written down.
        decision = ConflictJudge().should_judge(
            answer=model_answer, question=state.query or ""
        )
        if not decision.get("judge", True):
            state.trace.append({
                "step": "check_conflicts",
                "skipped": True,
                "skip_reason": decision.get("reason", ""),
            })
            logger.info(
                f"Conflict check skipped: {decision.get('reason', '')}"
            )
            return

        # Build a compact KB evidence digest (top 3 chunks, truncated)
        kb_parts: list[str] = []
        for chunk in state.evidence[:3]:
            text = (chunk.get("text") or "").strip()
            if text:
                kb_parts.append(text[:600])
        kb_evidence = "\n\n---\n\n".join(kb_parts) if kb_parts else "(no evidence)"

        detection: dict[str, Any] = {"verdict": "unrelated", "conflict_claims": []}
        try:
            detection = await detect_conflict(
                llm=self.llm,
                llm_answer=state.answer,
                kb_evidence=kb_evidence,
                redactor=self.redactor,
            )
        except Exception as e:
            logger.exception(f"Conflict detection crashed: {e}")
            detection = {"verdict": "unrelated", "conflict_claims": [], "failed": True}

        verdict = detection.get("verdict", "unrelated")
        claims = detection.get("conflict_claims") or []
        explanation = detection.get("explanation", "")
        check_failed = bool(detection.get("failed"))

        # Heuristic: scan the ANSWER for the conflict markers the answer
        # prompt asks the model to add. This catches the case where the
        # LLM already noticed an issue while answering (per answer.j2
        # instruction) and added a warning, but the separate detector
        # returned "consistent" because the answer text follows the KB
        # wording.
        #
        # Only the answer is scanned, never the evidence: the knowledge
        # base is supposed to *contain* the words "过时" / "冲突" - that
        # is what a conflict looks like - so scanning it would invent a
        # conflict out of every document that merely discusses one, and
        # would override the detector's own "consistent" verdict.
        answer_text = state.answer or ""
        markers = [
            "⚠️",
            "通用知识与知识库存在分歧",
            "outdated",
            "过时",
            "已弃用",
            "deprecated",
        ]
        heuristic_hit = next((m for m in markers if m.lower() in answer_text.lower()), None)

        state.trace.append({
            "step": "check_conflicts",
            "verdict": verdict,
            "claim_count": len(claims),
            "heuristic_hit": heuristic_hit,
            "check_failed": check_failed,
            "skipped": False,
            "skip_reason": "",
        })

        # Promote heuristic findings to a real conflict record
        if heuristic_hit and verdict != "conflict":
            logger.debug(f"Heuristic conflict marker detected: {heuristic_hit!r}")
            first_citation = state.citations[0] if state.citations else {}
            heuristic_claim = {
                "topic": f"知识库标记为{heuristic_hit}",
                "llm_says": "（基于知识库原文，已在答案中标注过时）",
                "kb_says": f"证据来源含 {heuristic_hit} 标记",
            }
            claims = [heuristic_claim]
            verdict = "conflict"
            explanation = (
                f"启发式检测：证据含冲突标记 {heuristic_hit!r}，"
                "LLM-as-detector 独立判定为 consistent，但 prompt 模板已提示差异。"
            )

        if check_failed:
            # The check did not run, so we cannot rule out that the
            # answer contradicts the documents. The knowledge base is
            # the answer then - and the model's own wording is not
            # shown, because nothing has checked it (AC-E-010).
            state.answer = rewrite_with_knowledge_base(
                model_answer=model_answer,
                evidence=state.evidence,
                citations=state.citations,
                conflicts=[],
                check_failed=True,
            )
            return

        if verdict != "conflict" or not claims:
            return

        # Attach citation context per claim (best-effort)
        first_citation = state.citations[0] if state.citations else {}
        for claim in claims:
            conflict_obj = {
                "type": "llm_vs_kb",
                "topic": claim.get("topic", "N/A"),
                # The field names are the response model's, so the two
                # sides actually reach the client instead of being
                # dropped on the way out.
                "llm_answer": claim.get("llm_says", ""),
                "kb_evidence": claim.get("kb_says", ""),
                "citation": first_citation,
                "message": format_conflict_message({"verdict": "conflict", "conflict_claims": [claim]}),
                "explanation": explanation,
            }
            state.conflicts.append(conflict_obj)

        # Persist each claim to the conflicts table (best-effort)
        if self.consolidation is not None:
            first_chunk_id = (
                state.evidence[0].get("id") if state.evidence else None
            )
            for conflict_obj in state.conflicts:
                try:
                    await self.consolidation.log_conflict(
                        llm_answer=conflict_obj["llm_answer"],
                        kb_evidence=conflict_obj["kb_evidence"],
                        episode_id=state.assistant_episode_id,
                        knowledge_chunk_id=first_chunk_id,
                        resolution=conflict_obj["explanation"],
                        topic=conflict_obj["topic"],
                        citation=json.dumps(
                            conflict_obj["citation"], ensure_ascii=False
                        ) if conflict_obj["citation"] else None,
                    )
                except Exception as e:
                    logger.warning(f"Failed to persist conflict: {e}")

        # The knowledge base is the answer (ADR-010). The model's own
        # wording goes in as a comparison, at the end, clearly labelled.
        state.answer = rewrite_with_knowledge_base(
            model_answer=model_answer,
            evidence=state.evidence,
            citations=state.citations,
            conflicts=claims,
        )

    async def _record_episode_safe(self, state: AgentState) -> None:
        """Record this turn in episodic memory (best-effort, never raises)."""
        if self.consolidation is None:
            return
        try:
            topic_tags = []
            entities = []
            if state.understanding:
                topic = state.understanding.get("topic")
                if topic:
                    topic_tags.append(topic)
                entity_hints = state.understanding.get("entity_hints") or []
                if isinstance(entity_hints, list):
                    entities.extend(str(e) for e in entity_hints[:10])

            # Pull a topic tag from the conflict type if present
            if state.conflicts:
                topic_tags.append("conflict-detected")

            state.user_episode_id = await self.consolidation.record_episode(
                session_id=state.session_id,
                role="user",
                content=state.query,
                topic_tags=list(dict.fromkeys(topic_tags)),
                entities=entities,
                importance_score=0.5,
            )
            if state.answer and not state.refused:
                state.assistant_episode_id = await self.consolidation.record_episode(
                    session_id=state.session_id,
                    role="assistant",
                    content=state.answer,
                    topic_tags=list(dict.fromkeys(topic_tags + ["answer"])),
                    entities=entities,
                    importance_score=0.6,
                )
        except Exception as e:
            logger.warning(f"Failed to record episode: {e}")

    def _schedule_consolidation(self) -> None:
        """Let the consolidation layer decide whether this turn ends a round.

        Two of the three triggers live here: enough turns have piled up
        (ADR-015). The third - idle - is watched in the background, and
        the user can always ask for a round by hand.

        The round runs in the background: it is a batch job and must not
        sit on the path of the answer.
        """
        if self.consolidation is None:
            return
        schedule = getattr(self.consolidation, "schedule_consolidation", None)
        if schedule is None:
            return
        try:
            schedule()
        except Exception as e:  # noqa: BLE001 - a lost round is not a broken chat
            logger.warning(f"Could not schedule a consolidation round: {e}")
