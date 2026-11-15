"""Confidence scoring for retrieval results.

Computes a 0-1 confidence score based on:
- Top similarity score
- Score gap between top-1 and top-2
- Number of supporting evidence

The score is a *signal*, not a decision. Whether the system may answer
at all is decided by the evidence gate (`yuanwork.retrieval.evidence_gate`):
a hard rule gate and a model self-assessment gate, and the more
conservative of the two wins (ADR-008).

The single numeric threshold this module used to apply is gone. It
almost never fired - the system refused on 0 or 1 pieces of evidence and
answered on everything else, however thin the evidence was - so the
decision moved to gates that can actually catch it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ConfidenceScorer:
    """Score the retrieval results so the user can see how solid they are.

    The score is calibrated against the *fused* score's range, not the
    dense similarity's. The two channels are each normalised against
    their own maximum and then added up, so the best hit lands between
    the larger weight - only one channel found it - and the sum of the
    two - both channels found it at the top. Calibrating against the
    dense range instead would report a strong hit as a weak one, which
    is what happened before the weights became configuration (ADR-006).
    """

    def __init__(
        self,
        high_threshold: float | None = None,
        low_threshold: float | None = None,
        alpha: float | None = None,
        beta: float | None = None,
    ):
        from yuanwork.config import get_settings

        settings = get_settings()
        if high_threshold is None:
            high_threshold = settings.confidence_threshold_high
        if low_threshold is None:
            low_threshold = settings.confidence_threshold_low
        if alpha is None:
            alpha = settings.fusion_alpha
        if beta is None:
            beta = settings.fusion_beta
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold
        # The best hit: the larger weight if only one channel found it,
        # the sum of both if they agree.
        self.score_floor = max(alpha, beta)
        self.score_ceiling = alpha + beta
        self.score_span = max(self.score_ceiling - self.score_floor, 1e-6)

    def score(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute confidence score.

        Returns dict with:
        - confidence: 0-1 score
        - level: 'high' | 'medium' | 'low'
        - top_score: best similarity score
        - score_gap: top1 - top2
        - evidence_count: number of chunks

        The score says nothing about whether the answer may be given.
        That is the evidence gate's job.
        """
        if not results:
            return {
                "confidence": 0.0,
                "level": "low",
                "top_score": 0.0,
                "score_gap": 0.0,
                "evidence_count": 0,
            }

        scores = [r.get("_score", 0.0) for r in results]
        top_score = scores[0]
        second_score = scores[1] if len(scores) > 1 else 0.0
        score_gap = top_score - second_score

        # The top hit, on the fused score's own scale.
        norm_top = min(1.0, max(0.0, (top_score - self.score_floor) / self.score_span))

        # The gap, on the same scale: a gap of half the range is a gap
        # of half the scale.
        norm_gap = min(1.0, max(0.0, score_gap / self.score_span))

        # Evidence count bonus
        evidence_bonus = 1.0 if len(results) >= 3 else 0.5

        # Weighted combination
        confidence = (
            0.4 * norm_top
            + 0.3 * norm_gap
            + 0.2 * evidence_bonus
            + 0.1 * min(1.0, len(results) / 5.0)
        )
        confidence = min(1.0, max(0.0, confidence))

        # Determine level
        if confidence >= self.high_threshold:
            level = "high"
        elif confidence >= self.low_threshold:
            level = "medium"
        else:
            level = "low"

        return {
            "confidence": round(confidence, 3),
            "level": level,
            "top_score": round(top_score, 3),
            "score_gap": round(score_gap, 3),
            "evidence_count": len(results),
        }
