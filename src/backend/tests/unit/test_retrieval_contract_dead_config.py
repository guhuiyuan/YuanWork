"""The settings that shape the retrieval.

They used to be declared and never read - a promise the code did not
keep. Now they are wired, and this pins that fact: change one of them
and the retrieval has to notice.

The general rule (every declared setting is read somewhere) is pinned in
``test_runtime_contract_validation.py``.
"""

from __future__ import annotations

import inspect

from yuanwork.config import YuanWorkSettings
from yuanwork.retrieval.confidence import ConfidenceScorer
from yuanwork.retrieval.hybrid import HybridRetriever


def _tmp():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(prefix="yuanwork_cfg_"))


# --------------------------------------------------------------------------
# the recall sizes
# --------------------------------------------------------------------------


def test_the_recall_size_comes_from_the_configuration(monkeypatch):
    """The retriever reads the settings singleton - so the value has to
    be in the environment."""
    from yuanwork.config import reset_settings

    monkeypatch.setenv("YUANWORK_TOP_K_INITIAL", "7")
    monkeypatch.setenv("YUANWORK_TOP_K_RERANK", "3")
    reset_settings()

    retriever = HybridRetriever(semantic_memory=object())  # type: ignore[arg-type]
    assert retriever.top_k_initial == 7
    assert retriever.top_k_rerank == 3

    reset_settings()


def test_the_recall_size_is_fifty_by_default():
    retriever = HybridRetriever(semantic_memory=object())  # type: ignore[arg-type]
    assert retriever.top_k_initial == 50


# --------------------------------------------------------------------------
# the reranker switch
# --------------------------------------------------------------------------


def test_the_reranker_switch_comes_from_the_configuration():
    """The cross-encoder is off by default - it costs 568 MB and 5-30 s
    on the first use."""
    settings = YuanWorkSettings(data_dir=_tmp(), use_reranker=False)
    assert settings.use_reranker is False

    retriever = HybridRetriever(semantic_memory=object())  # type: ignore[arg-type]
    assert retriever.use_reranker_flag is False
    assert retriever.reranker is None


def test_the_reranker_is_created_when_it_is_switched_on():
    retriever = HybridRetriever(
        semantic_memory=object(),  # type: ignore[arg-type]
        use_reranker=True,
    )

    assert retriever.use_reranker_flag is True
    assert retriever.reranker is not None


def test_the_orchestrator_builds_the_retriever_without_a_reranker():
    """The orchestrator does not pass one - the retriever reads the
    setting itself, so there is one place to change."""
    from yuanwork.orchestrator import agent

    source = inspect.getsource(agent)
    assert "HybridRetriever(semantic_memory)" in source
    assert "reranker=" not in source


def test_the_reranker_model_setting_is_read(monkeypatch):
    """`settings.reranker_model` decides which cross-encoder is loaded."""
    from yuanwork.config import reset_settings

    monkeypatch.setenv("YUANWORK_RERANKER_MODEL", "some/other-model")
    monkeypatch.setenv("YUANWORK_USE_RERANKER", "true")
    reset_settings()

    retriever = HybridRetriever(semantic_memory=object())  # type: ignore[arg-type]
    assert retriever.reranker.model_name == "some/other-model"

    reset_settings()


# --------------------------------------------------------------------------
# the embedding batch size
# --------------------------------------------------------------------------


def test_the_embedding_batch_size_setting_is_read(monkeypatch, tmp_path):
    """The batch size is the biggest lever on how long the first build
    takes, so it cannot stay a constant."""
    from yuanwork.config import reset_settings
    from yuanwork.memory.semantic import SemanticMemory

    monkeypatch.setenv("YUANWORK_EMBEDDING_BATCH_SIZE", "3")
    reset_settings()

    memory = SemanticMemory(data_dir=tmp_path / "lancedb")
    assert memory.embedding_batch_size == 3

    reset_settings()


# --------------------------------------------------------------------------
# the confidence thresholds
# --------------------------------------------------------------------------


def test_the_confidence_thresholds_come_from_the_configuration(monkeypatch):
    from yuanwork.config import reset_settings

    monkeypatch.setenv("YUANWORK_CONFIDENCE_THRESHOLD_HIGH", "0.99")
    monkeypatch.setenv("YUANWORK_CONFIDENCE_THRESHOLD_LOW", "0.95")
    reset_settings()

    scorer = ConfidenceScorer()
    assert scorer.high_threshold == 0.99
    assert scorer.low_threshold == 0.95

    reset_settings()
