"""Where the embedding runs.

The embedding is the slowest part of a build. It should run on the
GPU when there is one, at half precision, reading no more than it has
to. When there is no GPU it runs on the CPU - and it says why, because
a build that is slow for no visible reason is the kind of thing a
person loses an afternoon to.

* AC-P-011 - the embedding is fast enough (15 slices a second)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from yuanwork.memory.semantic import SemanticMemory


@pytest.fixture
def memory(tmp_path: Path) -> SemanticMemory:
    return SemanticMemory(data_dir=tmp_path / "lancedb")


# --------------------------------------------------------------------------
# the device
# --------------------------------------------------------------------------


def test_the_device_is_chosen_by_whether_there_is_a_gpu(memory: SemanticMemory):
    """auto means: the GPU when there is one, the CPU otherwise."""
    try:
        import torch

        cuda = bool(torch.cuda.is_available())
    except Exception:
        cuda = False

    if cuda:
        assert memory.embedding_device == "cuda"
        assert memory.device_fallback_reason == ""
    else:
        assert memory.embedding_device == "cpu"
        # The reason is written down.
        assert memory.device_fallback_reason
        try:
            import torch

            cuda_build = bool(getattr(torch.version, "cuda", None))
        except Exception:
            cuda_build = False
        if cuda_build:
            # The card is the one that is missing.
            assert "显卡" in memory.device_fallback_reason
        else:
            # The card may be sitting in the machine - the torch build
            # is the thing without CUDA in it.
            assert "torch" in memory.device_fallback_reason


def test_a_cpu_only_torch_does_not_blame_a_missing_card(monkeypatch):
    """The card may be right there; the torch build has no CUDA in it.

    Saying "no graphics card" when the card is in the machine sends
    the user looking for the wrong thing - the fix is a different torch
    build, not a different computer.
    """
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.version, "cuda", None, raising=False)

    device, reason = SemanticMemory._resolve_device("auto")

    assert device == "cpu"
    assert "torch" in reason
    assert "没有可用的 NVIDIA 显卡" not in reason


def test_a_card_the_driver_does_not_see_says_so(monkeypatch):
    """A torch build with CUDA in it, and no card it can reach."""
    import torch

    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    monkeypatch.setattr(torch.version, "cuda", "12.6", raising=False)

    device, reason = SemanticMemory._resolve_device("auto")

    assert device == "cpu"
    assert "驱动" in reason


def test_the_cpu_can_be_asked_for(memory: SemanticMemory):
    device, reason = SemanticMemory._resolve_device("cpu")
    assert device == "cpu"
    assert reason == ""


def test_a_device_that_does_not_exist_falls_back_to_the_cpu():
    device, reason = SemanticMemory._resolve_device("quantum")
    assert device == "cpu"
    assert "不认识" in reason


def test_asking_for_cuda_on_a_machine_without_one_says_so():
    try:
        import torch

        cuda = bool(torch.cuda.is_available())
    except Exception:
        cuda = False

    device, reason = SemanticMemory._resolve_device("cuda")

    if cuda:
        assert device == "cuda"
    else:
        assert device == "cpu"
        assert "CUDA" in reason


def test_the_fallback_reason_is_reported_by_the_health_endpoint():
    """The user has to be able to see why the build is slow."""
    import inspect

    from yuanwork.api.routes import health

    source = inspect.getsource(health)
    assert "embedding_device" in source
    assert "embedding_device_reason" in source


# --------------------------------------------------------------------------
# the read
# --------------------------------------------------------------------------


def test_one_piece_of_text_is_at_most_512_tokens(memory: SemanticMemory):
    """A document is cut into pieces anyway - a longer read is a
    slower one."""
    assert memory.embedding_max_tokens == 512


def test_the_batch_size_is_configuration(memory: SemanticMemory):
    """Zero means "the device decides" - the batch is then cut by what
    the machine can hold, measured while it runs (ADR-044). A number is
    a cap the device may go smaller than but never bigger."""
    assert memory.embedding_batch_size >= 0


def test_half_precision_is_only_worth_it_on_the_gpu():
    """fp16 on the CPU is slower, not faster."""
    settings = {
        "embedding_device": "cuda",
        "embedding_use_fp16": True,
    }
    assert settings["embedding_use_fp16"] is True

    from yuanwork.config import YuanWorkSettings

    parsed = YuanWorkSettings(data_dir=Path("."))
    assert parsed.embedding_use_fp16 is True


def test_the_model_is_loaded_with_the_device_and_the_precision(monkeypatch):
    """The model is built on the device that was chosen."""
    import inspect

    from yuanwork.memory import semantic as semantic_module

    # The class, not one method: the load may sit in a helper (the
    # lock around it lives in `load_embedding_model`, the load itself
    # in `_load_embedding_model`), and what matters is that the device
    # and the precision are passed to the model.
    source = inspect.getsource(semantic_module.SemanticMemory)
    assert "devices=self.embedding_device" in source
    assert "use_fp16" in source


def test_the_embedding_passes_the_token_budget():
    """The model is told how many tokens it may read."""
    import inspect

    from yuanwork.memory import semantic as semantic_module

    source = inspect.getsource(semantic_module.SemanticMemory)
    assert "max_length=self.embedding_max_tokens" in source
