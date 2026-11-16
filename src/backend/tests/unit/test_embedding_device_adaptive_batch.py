"""The batch the embedding gets is cut by what the device can take.

Three things are checked here:

* the cutting itself - a batch stays within the length ratio, stays
  within the token budget, and stays within the number of slices
* the device decides - the batch grows into what the machine can hold,
  and shrinks when the machine says it cannot take it
* the library's own path is still there - for an embedder whose
  internals do not look the way the encoder expects
"""

from __future__ import annotations

import numpy as np
import pytest
import torch

from yuanwork.memory.encoder import (
    LENGTH_RATIO,
    MAX_SEQUENCES,
    MIN_SEQUENCES,
    OutOfMemory,
    SlicedEncoder,
    cut_batches,
    device_profile,
    pack_batches,
)


# --------------------------------------------------------------------------
# the cutting
# --------------------------------------------------------------------------


def test_a_batch_keeps_its_lengths_within_the_ratio():
    lengths = [100, 120, 130, 900, 1000, 3000]
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for batch in pack_batches(lengths, order, token_budget=10_000_000):
        shortest = min(lengths[i] for i in batch)
        longest = max(lengths[i] for i in batch)
        assert longest <= shortest * LENGTH_RATIO


def test_a_batch_stays_within_the_token_budget():
    lengths = [400] * 20
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for batch in pack_batches(lengths, order, token_budget=2000):
        longest = max(lengths[i] for i in batch)
        assert len(batch) * longest <= 2000


def test_a_batch_stays_within_the_number_of_slices():
    lengths = [10] * 100
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    for batch in pack_batches(lengths, order, token_budget=10_000_000, max_sequences=16):
        assert len(batch) <= 16


def test_every_slice_ends_up_in_exactly_one_batch():
    lengths = [50, 60, 70, 500, 600, 2000]
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    batches = pack_batches(lengths, order, token_budget=1000)
    flat = [index for batch in batches for index in batch]
    assert sorted(flat) == list(range(len(lengths)))


def test_the_budget_is_asked_for_again_for_every_batch():
    """A device that has just said 'that was too much' makes the batches
    after it smaller."""
    lengths = [100] * 40
    order = sorted(range(len(lengths)), key=lambda i: lengths[i])
    asked: list[int] = []

    def budget() -> int:
        asked.append(len(asked))
        # The first batch sees a small budget, the one after it a big one.
        return 1000 if len(asked) == 1 else 100_000

    batches = list(cut_batches(lengths, order, budget, max_sequences=100))
    assert len(asked) >= 2
    # The first batch is the small one.
    assert len(batches[0]) <= 10
    assert len(batches[-1]) > len(batches[0])


# --------------------------------------------------------------------------
# the device
# --------------------------------------------------------------------------


def test_the_cpu_profile_says_what_the_machine_has():
    profile = device_profile("cpu")
    assert profile.kind == "cpu"
    assert profile.cores >= 1
    assert profile.total_bytes > 0
    assert profile.budget_bytes > 0


def test_the_cuda_profile_asks_the_card():
    profile = device_profile("cuda")
    if torch.cuda.is_available():
        assert profile.kind == "cuda"
        assert profile.total_bytes > 0
        assert profile.budget_bytes <= profile.total_bytes


# --------------------------------------------------------------------------
# a stand-in for the model
# --------------------------------------------------------------------------


class FakeTokenizer:
    pad_token_id = 0

    def __call__(self, texts, truncation=True, max_length=512, padding=False):
        ids = []
        for text in texts:
            length = min(len(text), max_length - 1)
            ids.append([1] + [2] * length)
        return {"input_ids": ids, "attention_mask": [[1] * len(i) for i in ids]}


class FakeModel:
    """A model that counts the characters it was given."""

    def __init__(self, limit: int | None = None):
        self.limit = limit
        #: The sizes of the batches that went through.
        self.calls: list[int] = []
        #: The sizes of the batches that were refused.
        self.refused: list[int] = []

    def __call__(self, text_input, return_dense=True, **kwargs):
        rows = text_input["input_ids"].shape[0]
        if self.limit and rows > self.limit:
            self.refused.append(rows)
            raise RuntimeError("CUDA out of memory")
        self.calls.append(rows)
        out = torch.zeros((rows, 1024), dtype=torch.float32)
        for row in range(rows):
            out[row, 0] = float(text_input["input_ids"][row].sum())
        return {"dense_vecs": out}


class FakeEmbedder:
    def __init__(self, limit: int | None = None):
        self.tokenizer = FakeTokenizer()
        self.model = FakeModel(limit=limit)
        self.encode_calls: list[int] = []

    def encode(self, sentences, return_dense=True, return_sparse=False, **kwargs):
        self.encode_calls.append(len(sentences))
        return {"dense_vecs": [[0.0] * 1024 for _ in sentences]}


def make_encoder(limit: int | None = None, device: str = "cpu") -> SlicedEncoder:
    embedder = FakeEmbedder(limit=limit)
    return SlicedEncoder(embedder, device, max_tokens=64)


def test_the_vectors_come_back_in_the_order_they_went_in():
    encoder = make_encoder()
    texts = ["短", "中等长度的一段文字", "很长" * 40]
    vectors = encoder.encode(texts)
    assert vectors.shape == (3, 1024)
    # Every vector carries the length of its own text: the stand-in
    # tokenizer gives one token for the text and one for each character.
    for index, text in enumerate(texts):
        expected = 1 + 2 * min(len(text), 63)
        assert vectors[index][0] == float(expected)


def test_the_library_is_not_called_when_the_encoder_can_do_the_work():
    embedder = FakeEmbedder()
    encoder = SlicedEncoder(embedder, "cpu", max_tokens=64)
    encoder.encode(["一段文字"])
    assert embedder.encode_calls == []
    assert embedder.model.calls, "the model was never called"


def test_the_library_is_used_when_the_embedder_has_no_internals():
    class Plain:
        def encode(self, sentences, return_dense=True, return_sparse=False, **kwargs):
            return {"dense_vecs": [[1.0, 2.0] for _ in sentences]}

    encoder = SlicedEncoder(Plain(), "cpu", max_tokens=64)
    assert not encoder.can_encode_here()
    vectors = encoder.encode(["一段", "两段"])
    assert vectors.shape == (2, 2)


def test_a_device_that_cannot_take_the_batch_gets_smaller_batches():
    """The batch is cut in half, and the batches after it are smaller."""
    encoder = make_encoder(limit=4)
    encoder._token_budget = 100_000
    texts = ["x" * 10 for _ in range(32)]
    vectors = encoder.encode(texts)
    assert vectors.shape == (32, 1024)
    # The device refused the big ones; everything that went through fits.
    assert encoder._embedder.model.refused, "the device never said no"
    assert max(encoder._embedder.model.calls) <= 4


def test_the_batch_never_holds_more_than_the_cap():
    encoder = make_encoder()
    texts = ["x" * 5 for _ in range(50)]
    encoder.encode(texts, cap=7)
    assert max(encoder._embedder.model.calls) <= 7


def test_an_empty_list_makes_no_call():
    embedder = FakeEmbedder()
    encoder = SlicedEncoder(embedder, "cpu", max_tokens=64)
    vectors = encoder.encode([])
    assert vectors.shape[0] == 0
    assert embedder.model.calls == []


# --------------------------------------------------------------------------
# the store's side of it
# --------------------------------------------------------------------------


async def test_the_store_hands_back_arrays(tmp_path):
    from yuanwork.memory.semantic import SemanticMemory

    memory = SemanticMemory(data_dir=tmp_path / "lancedb")
    memory._embedder = FakeEmbedder()
    memory._encoder = SlicedEncoder(memory._embedder, "cpu", max_tokens=64)

    vectors = await memory.embed_arrays(["一段文字", "另一段"])
    assert isinstance(vectors, np.ndarray)
    assert vectors.shape == (2, 1024)


async def test_the_texts_still_come_back_as_lists(tmp_path):
    """The retrieval side asks for one vector and expects a list."""
    from yuanwork.memory.semantic import SemanticMemory

    memory = SemanticMemory(data_dir=tmp_path / "lancedb")
    memory._embedder = FakeEmbedder()
    memory._encoder = SlicedEncoder(memory._embedder, "cpu", max_tokens=64)

    dense, sparse = await memory.embed_texts(["一段文字"])
    assert isinstance(dense[0], list)
    assert len(dense[0]) == 1024
    assert sparse is None


async def test_the_batch_size_setting_is_a_cap_not_a_plan(tmp_path):
    """Zero means the device decides; a number caps it."""
    from yuanwork.config import YuanWorkSettings, reset_settings
    from yuanwork.memory.semantic import SemanticMemory

    settings = YuanWorkSettings(data_dir=tmp_path / "data", embedding_batch_size=0)
    assert settings.embedding_batch_size == 0
    reset_settings()
    try:
        import yuanwork.config as config_module

        config_module._settings = settings
        memory = SemanticMemory(data_dir=tmp_path / "lancedb")
        memory._embedder = FakeEmbedder()
        memory._encoder = SlicedEncoder(memory._embedder, "cpu", max_tokens=64)
        await memory.embed_arrays(["x" * 5 for _ in range(20)])
        assert memory._encoder._sequence_cap is None
    finally:
        reset_settings()


def test_the_encoder_knows_what_the_device_is():
    encoder = make_encoder()
    assert encoder.profile.kind in ("cpu", "cuda")
    assert encoder.profile.cores >= 1


def test_the_limits_are_sane():
    assert MIN_SEQUENCES >= 1
    assert MAX_SEQUENCES >= MIN_SEQUENCES
    assert LENGTH_RATIO >= 1.0
