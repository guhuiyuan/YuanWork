"""Confidence scoring.

The score is a signal, not a decision: the evidence gate decides whether
the answer may be given (ADR-008), and its own contract is pinned in
``test_evidence_gate_hard_rules.py``. What is pinned here is the score
itself:

* the four weighted terms
* the level thresholds
* the calibration against the *fused* score's range
* the shape of the returned dict
"""

from __future__ import annotations

from yuanwork.retrieval.confidence import ConfidenceScorer


def results(*scores: float) -> list[dict]:
    return [{"_score": s} for s in scores]


# --------------------------------------------------------------------------
# the formula
# --------------------------------------------------------------------------


def test_the_default_thresholds():
    scorer = ConfidenceScorer()
    assert scorer.high_threshold == 0.7
    assert scorer.low_threshold == 0.4


def test_an_empty_result_set_scores_zero():
    out = ConfidenceScorer().score([])
    assert out["confidence"] == 0.0
    assert out["level"] == "low"
    assert out["evidence_count"] == 0


def test_the_score_is_the_weighted_sum_of_four_terms():
    """confidence = 0.4*norm_top + 0.3*norm_gap + 0.2*bonus + 0.1*count"""
    scorer = ConfidenceScorer()
    # The default weights are 0.65 / 0.35, so the best hit lands
    # between 0.65 (one channel found it) and 1.0 (both did).
    assert scorer.score_floor == 0.65
    assert scorer.score_ceiling == 1.0
    assert scorer.score_span == 0.35

    out = scorer.score(results(1.0, 0.6, 0.5))
    # norm_top = (1.0 - 0.65) / 0.35 = 1.0
    # norm_gap = (1.0 - 0.6) / 0.35 = 1.0 (clamped)
    # 3 results -> bonus = 1.0 ; 3/5 -> 0.6
    expected = 0.4 * 1.0 + 0.3 * 1.0 + 0.2 * 1.0 + 0.1 * 0.6
    assert out["confidence"] == round(expected, 3)
    assert out["level"] == "high"


def test_the_calibration_follows_the_fusion_weights():
    """The weights are configuration - the calibration follows them."""
    scorer = ConfidenceScorer(alpha=0.5, beta=0.5)

    assert scorer.score_floor == 0.5
    assert scorer.score_ceiling == 1.0
    assert scorer.score_span == 0.5


def test_a_hit_only_one_channel_found_is_at_the_floor():
    """A hit only one channel found is not a strong hit - it sits at
    the bottom of the fused range."""
    scorer = ConfidenceScorer()

    at_floor = scorer.score(results(0.65, 0.64, 0.63))
    at_ceiling = scorer.score(results(1.0, 0.99, 0.98))

    assert at_floor["confidence"] < at_ceiling["confidence"]
    assert at_floor["level"] == "low"


def test_a_hit_both_channels_found_scores_full():
    scorer = ConfidenceScorer()

    out = scorer.score(results(1.0, 0.6, 0.5))

    assert out["confidence"] > 0.8


# --------------------------------------------------------------------------
# the levels
# --------------------------------------------------------------------------


def test_the_level_boundaries():
    scorer = ConfidenceScorer()
    high = scorer.score(results(1.0, 0.9, 0.85))
    assert high["level"] == "high"

    medium = scorer.score(results(0.85, 0.6, 0.5))
    assert medium["level"] in ("medium", "high")

    low = scorer.score(results(0.65, 0.64, 0.63))
    assert low["level"] == "low"


def test_the_thresholds_can_be_overridden():
    scorer = ConfidenceScorer(high_threshold=0.99, low_threshold=0.95)
    out = scorer.score(results(1.0, 0.6, 0.5))
    # confidence is 0.96 - below the raised high threshold, above the
    # raised low threshold
    assert out["level"] == "medium"

    # the same numbers with the default thresholds read higher
    assert ConfidenceScorer().score(results(1.0, 0.6, 0.5))["level"] == "high"


# --------------------------------------------------------------------------
# the shape of the result
# --------------------------------------------------------------------------


def test_the_scorer_never_refuses_by_itself():
    """A single numeric threshold is not a decision any more.

    It almost never fired, so the system answered on whatever came
    back - which is the failure the double gate replaces.
    """
    out = ConfidenceScorer().score(results(0.31, 0.31, 0.30, 0.30, 0.29))

    assert "should_refuse" not in out
    assert "reason" not in out


def test_the_score_is_still_reported_for_a_weak_result_set():
    out = ConfidenceScorer().score(results(0.31, 0.31, 0.30, 0.30, 0.29))
    assert out["level"] == "low"
    assert out["evidence_count"] == 5


# --------------------------------------------------------------------------
# the input shape
# --------------------------------------------------------------------------


def test_the_first_result_is_taken_as_the_best():
    """The scorer assumes the input is sorted; it does not sort."""
    out = ConfidenceScorer().score(results(0.2, 0.9))
    assert out["top_score"] == 0.2


def test_a_result_without_a_score_counts_as_zero():
    out = ConfidenceScorer().score([{}, {}])
    assert out["top_score"] == 0.0
    assert out["evidence_count"] == 2


def test_the_evidence_count_is_the_number_of_results():
    out = ConfidenceScorer().score(results(0.9, 0.5, 0.4, 0.3))
    assert out["evidence_count"] == 4
