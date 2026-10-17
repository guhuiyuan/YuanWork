"""Confidence scoring for retrieval results.

Computes a 0-1 confidence score based on:
- Top similarity score
- Score gap between top-1 and top-2
- Number of supporting evidence
- LLM self-evaluation (optional)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class ConfidenceScorer:
    """Score retrieval confidence to decide whether to answer or refuse."""

    def __init__(
        self,
        high_threshold: float = 0.7,
        low_threshold: float = 0.4,
    ):
        self.high_threshold = high_threshold
        self.low_threshold = low_threshold

    def score(self, results: list[dict[str, Any]]) -> dict[str, Any]:
        """Compute confidence score.

        Returns dict with:
        - confidence: 0-1 score
        - level: 'high' | 'medium' | 'low'
        - top_score: best similarity score
        - score_gap: top1 - top2
        - evidence_count: number of chunks
        - should_refuse: bool
        """
        if not results:
            return {
                "confidence": 0.0,
                "level": "low",
                "top_score": 0.0,
                "score_gap": 0.0,
                "evidence_count": 0,
                "should_refuse": True,
                "reason": "no_results",
            }

        scores = [r.get("_score", 0.0) for r in results]
        top_score = scores[0]
        second_score = scores[1] if len(scores) > 1 else 0.0
        score_gap = top_score - second_score

        # Heuristic scoring (stage 1)
        # Normalize top_score (LanceDB similarity is 1/(1+distance), so range ~0.3-0.95)
        norm_top = min(1.0, max(0.0, (top_score - 0.3) / 0.6))

        # Gap score (0-1)
        norm_gap = min(1.0, score_gap / 0.2)

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

        should_refuse = confidence < self.low_threshold or len(results) < 2

        return {
            "confidence": round(confidence, 3),
            "level": level,
            "top_score": round(top_score, 3),
            "score_gap": round(score_gap, 3),
            "evidence_count": len(results),
            "should_refuse": should_refuse,
            "reason": "low_confidence" if should_refuse else None,
        }
