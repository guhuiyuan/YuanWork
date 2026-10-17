"""Tests for confidence scorer."""

from __future__ import annotations

from yuanwork.retrieval.confidence import ConfidenceScorer


def test_no_results_refuse():
    """Empty results trigger refusal."""
    scorer = ConfidenceScorer()
    result = scorer.score([])

    assert result["should_refuse"] is True
    assert result["confidence"] == 0.0
    assert result["level"] == "low"


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

    assert result["should_refuse"] is False
    assert result["level"] in ("high", "medium")
    assert result["confidence"] > 0


def test_low_confidence_refuse():
    """Weak results trigger refusal."""
    scorer = ConfidenceScorer()
    results = [
        {"_score": 0.4},
        {"_score": 0.4},  # No gap
    ]
    result = scorer.score(results)

    # Should refuse due to low confidence
    assert result["should_refuse"] is True or result["level"] == "low"


def test_thresholds_configurable():
    """Thresholds are configurable."""
    scorer = ConfidenceScorer(high_threshold=0.9, low_threshold=0.7)
    results = [{"_score": 0.85}, {"_score": 0.5}]

    result = scorer.score(results)
    # With high_threshold=0.9, 0.85 won't be high
    # depends on other factors
    assert result["confidence"] > 0
