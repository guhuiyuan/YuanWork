"""AgentLoop - 3-step reasoning state machine.

Pipeline:
  Step 1 (Understand): Parse user query into structured intent
  Step 2 (Localize):  Identify candidate files
  Step 3 (Extract):    Get relevant chunks within candidates
  Step 4 (Answer):    Synthesize answer with citations, or refuse
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator

from yuanwork.api.schemas import ChatMessage
from yuanwork.llm.gateway import LLMGateway
from yuanwork.memory.episodic import EpisodicMemory
from yuanwork.memory.semantic import SemanticMemory
from yuanwork.orchestrator.steps.understand import understand_query
from yuanwork.orchestrator.steps.localize import localize_files
from yuanwork.orchestrator.steps.extract import extract_evidence
from yuanwork.orchestrator.steps.answer import generate_answer, generate_refusal
from yuanwork.retrieval.confidence import ConfidenceScorer
from yuanwork.retrieval.hybrid import HybridRetriever

if TYPE_CHECKING:
    from yuanwork.consolidation.engine import ConsolidationEngine

logger = logging.getLogger(__name__)


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

    # Final outputs
    answer: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None

    # Memory bookkeeping
    user_episode_id: int | None = None
    assistant_episode_id: int | None = None

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
    ):
        self.llm = llm_gateway
        self.semantic = semantic_memory
        self.episodic = episodic_memory
        self.consolidation = consolidation_engine
        self.retriever = HybridRetriever(semantic_memory)
        self.scorer = ConfidenceScorer()

    async def run(
        self,
        query: str,
        session_id: str,
        history: list[ChatMessage] | None = None,
        opts: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run the agent loop and return the final state as a dict."""
        state = AgentState(
            query=query,
            session_id=session_id,
            history=history or [],
        )

        await self._step_understand(state)
        await self._step_localize(state)
        await self._step_extract(state)
        await self._step_answer(state)
        await self._step_check_conflicts(state)

        # Record episodic memory for this turn (best-effort, don't fail chat)
        await self._record_episode_safe(state)

        return {
            "answer": state.answer,
            "citations": state.citations,
            "confidence": state.confidence["confidence"] if state.confidence else 0.0,
            "refused": state.refused,
            "refusal_reason": state.refusal_reason,
            "conflicts": state.conflicts,
            "trace": state.trace,
        }

    async def stream(
        self,
        query: str,
        session_id: str,
        history: list[ChatMessage] | None = None,
        opts: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream events during agent execution."""
        state = AgentState(
            query=query,
            session_id=session_id,
            history=history or [],
        )

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

        # Step 4
        yield {"type": "trace", "data": {"step": "answer", "status": "started"}}
        await self._step_answer(state)
        yield {
            "type": "trace",
            "data": {
                "step": "answer",
                "status": "completed",
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
            yield {
                "type": "citations",
                "data": {"citations": state.citations},
            }
            if state.conflicts:
                yield {
                    "type": "conflict",
                    "data": {"conflicts": state.conflicts},
                }

        yield {"type": "done", "data": {"session_id": session_id}}

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
        state.evidence = await extract_evidence(
            query=state.query,
            candidates=state.candidates,
            retriever=self.retriever,
            top_k=10,
        )

        # Compute confidence
        state.confidence = self.scorer.score(state.evidence)

        state.trace.append({
            "step": "extract",
            "evidence_count": len(state.evidence),
            "confidence": state.confidence,
        })

    async def _step_answer(self, state: AgentState) -> None:
        """Step 4: Generate answer or refuse."""
        # Check if we should refuse
        if state.confidence and state.confidence.get("should_refuse"):
            state.refused = True
            state.refusal_reason = state.confidence.get("reason", "low_confidence")
            state.answer = await generate_refusal(state.candidates)
            state.citations = []
            state.trace.append({
                "step": "answer",
                "refused": True,
                "reason": state.refusal_reason,
            })
            return

        # Generate answer
        try:
            state.answer = await generate_answer(
                llm=self.llm,
                query=state.query,
                evidence=state.evidence,
                confidence=state.confidence or {},
            )
            state.refused = False
        except Exception as e:
            logger.exception(f"Answer generation failed: {e}")
            state.refused = True
            state.refusal_reason = f"generation_error: {e}"
            state.answer = "抱歉，生成回答时出现错误。请稍后重试。"
            state.trace.append({"step": "answer", "error": str(e)})
            return

        # Build citations from evidence
        state.citations = self._build_citations(state.evidence)

        state.trace.append({
            "step": "answer",
            "refused": False,
            "citation_count": len(state.citations),
        })

    def _build_citations(self, evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert evidence chunks into citation objects."""
        citations = []
        for idx, chunk in enumerate(evidence, start=1):
            meta = chunk.get("meta", {}) or {}
            citation = {
                "index": idx,
                "file_name": chunk.get("file_name", ""),
                "file_path": chunk.get("file_path", ""),
                "page": meta.get("page"),
                "slide_number": meta.get("slide_number"),
                "section": meta.get("heading") or meta.get("section"),
                "text": chunk.get("text", "")[:500],  # Truncate for display
                "chunk_id": chunk.get("id", ""),
            }
            citations.append(citation)
        return citations

    async def _step_check_conflicts(self, state: AgentState) -> None:
        """Step 5 (Stage 2): detect LLM-vs-KB conflicts.

        Runs only if we generated an answer (not refused) and have evidence.
        Persists detected conflicts to SQLite via the consolidation engine.
        """
        # Skip if refused or no usable answer/evidence
        if state.refused or not state.answer or not state.evidence:
            return

        from yuanwork.consolidation.conflict import detect_conflict, format_conflict_message

        # Build a compact KB evidence digest (top 3 chunks, truncated)
        kb_parts: list[str] = []
        for chunk in state.evidence[:3]:
            text = (chunk.get("text") or "").strip()
            if text:
                kb_parts.append(text[:600])
        kb_evidence = "\n\n---\n\n".join(kb_parts) if kb_parts else "(no evidence)"

        try:
            detection = await detect_conflict(
                llm=self.llm,
                llm_answer=state.answer,
                kb_evidence=kb_evidence,
            )
        except Exception as e:
            logger.exception(f"Conflict detection crashed: {e}")
            state.trace.append({"step": "check_conflicts", "error": str(e)})
            return

        verdict = detection.get("verdict", "unrelated")
        claims = detection.get("conflict_claims") or []
        explanation = detection.get("explanation", "")

        state.trace.append({
            "step": "check_conflicts",
            "verdict": verdict,
            "claim_count": len(claims),
        })

        if verdict != "conflict" or not claims:
            return

        # Attach citation context per claim (best-effort)
        first_citation = state.citations[0] if state.citations else {}
        for claim in claims:
            conflict_obj = {
                "type": "llm_vs_kb",
                "topic": claim.get("topic", "N/A"),
                "llm_says": claim.get("llm_says", ""),
                "kb_says": claim.get("kb_says", ""),
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
                        llm_answer=conflict_obj["llm_says"],
                        kb_evidence=conflict_obj["kb_says"],
                        episode_id=state.assistant_episode_id,
                        knowledge_chunk_id=first_chunk_id,
                        resolution=conflict_obj["explanation"],
                    )
                except Exception as e:
                    logger.warning(f"Failed to persist conflict: {e}")

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
