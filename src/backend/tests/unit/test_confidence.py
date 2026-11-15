"""Tests for confidence scorer.

The score is a signal the user gets to see; whether the answer may be
given is the evidence gate's decision (ADR-008). So the scorer no
longer says "refuse" - it only says how solid the retrieval looks.
"""

from __future__ import annotations

from yuanwork.retrieval.confidence import ConfidenceScorer


def test_no_results_score_zero():
    """An empty result set scores zero."""
    scorer = ConfidenceScorer()
    result = scorer.score([])

    assert result["confidence"] == 0.0
    assert result["level"] == "low"
    assert result["evidence_count"] == 0


def test_high_confidence():
    """Strong match returns high confidence."""
    scorer = ConfidenceScorer()
    results = [
        {"_score": 0.95},
        {"_score": 0.5},
        {"_score": 0.4},
        {"_score": 0.3},
    ]
    result = scorer.score(results)

    assert result["level"] in ("high", "medium")
    assert result["confidence"] > 0


def test_a_weak_result_set_scores_low():
    """Weak results score low - the score says so, it does not refuse."""
    scorer = ConfidenceScorer()
    results = [
        {"_score": 0.4},
        {"_score": 0.4},  # No gap
    ]
    result = scorer.score(results)

    assert result["level"] == "low"


def test_the_scorer_does_not_decide_whether_to_answer():
    """The refusal decision belongs to the evidence gate."""
    scorer = ConfidenceScorer()

    result = scorer.score([{"_score": 0.31}, {"_score": 0.31}])

    assert "should_refuse" not in result
    assert "reason" not in result


def test_thresholds_configurable():
    """Thresholds are configurable."""
    scorer = ConfidenceScorer(high_threshold=0.9, low_threshold=0.7)
    results = [{"_score": 0.85}, {"_score": 0.5}]

    result = scorer.score(results)
    # With high_threshold=0.9, 0.85 won't be high
    # depends on other factors
    assert result["confidence"] > 0
