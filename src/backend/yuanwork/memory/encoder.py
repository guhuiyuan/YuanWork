"""Turning slices into vectors, on whatever device the machine has.

Three things are done here that a plain call into the embedding library
does not do:

* **The work is not done twice.** The library's ``encode()`` first runs
  the model on a batch to find out how large a batch the device can
  take, throws that result away, and then runs the same batch again for
  real. On a build that is half the embedding time gone.

* **The batch follows the device, not a number in a config file.** A
  card with 6 GB and a card with 24 GB should not encode the same way,
  and a machine with no card should not either. The size of a batch is
  measured here: the first batch says how much memory one padded token
  costs on this device, and the batches after it are cut to fill the
  budget that leaves - and to leave room for the desktop, the browser
  and everything else the machine is doing.

* **The vectors stay in the array they were computed in.** Turning 1024
  floats per slice into Python objects and back again is pure waste;
  Arrow takes the array as it is.

The library's own path stays available as a fallback - for a version
of the library whose internals do not look the way this expects, and
for the sparse channel, which this path does not compute.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

#: How much of the device's memory one batch may use. The rest belongs
#: to the rest of the machine: a build that fills the card makes the
#: whole desktop stutter, and the user is the one who has to work
#: while it runs.
MEMORY_SHARE = 0.4

#: The most slices one forward pass may hold, whatever the device says.
#:
#: This one is not about "as much as fits" - it is about the shape of
#: the memory. The attention matrix of a batch grows with the **square**
#: of the longest slice, so a batch of 300 slices at 512 tokens wants
#: more memory than the whole card has (measured: the first attempt at
#: "fill the card" ran the 3060 out of memory).
#:
#: Thirty-two is where the speed stops improving anyway: measured on
#: real slices, 16 slices a batch gives 216.5 slices/second, 32 gives
#: 221.8, 64 gives 222.9, 128 gives 223.6 - the last half percent is
#: not worth the memory.
MAX_SEQUENCES = 32

#: The fewest slices one forward pass may hold. Below this the device
#: is not given enough work to keep itself busy.
MIN_SEQUENCES = 8

#: How much longer the longest slice in a batch may be than the
#: shortest. A batch is padded to its longest slice, so the slices in a
#: batch are kept within a ratio of each other; the ones that are too
#: different start a new batch.
LENGTH_RATIO = 3.0

#: What one padded token is assumed to cost before anything has been
#: measured. Deliberately pessimistic: the first batch then fits, and
#: the measurement replaces the guess.
BYTES_PER_TOKEN_GUESS = 16 * 1024


class OutOfMemory(RuntimeError):
    """The device could not take the batch."""


@dataclass(frozen=True)
class DeviceProfile:
    """What the device can do."""

    kind: str
    #: How much memory it has in total.
    total_bytes: int
    #: How much of it one batch may use.
    budget_bytes: int
    #: How many cores it has - on a CPU this is what does the work.
    cores: int
    #: What it is, in words, for the log.
    note: str


def device_profile(device: str) -> DeviceProfile:
    """What the device we are about to use can do."""
    kind = (device or "cpu").strip().lower()
    cores = physical_cores()

    if kind == "cuda":
        try:
            import torch

            properties = torch.cuda.get_device_properties(0)
            total = int(properties.total_memory)
            return DeviceProfile(
                kind="cuda",
                total_bytes=total,
                budget_bytes=max(256 * 1024 * 1024, int(total * MEMORY_SHARE)),
                cores=cores,
                note=f"{properties.name}，显存 {total / 1024 ** 3:.1f} GB",
            )
        except Exception as e:  # noqa: BLE001 - the profile is a best guess
            logger.warning("问不到显卡的显存，按 CPU 估：%r", e)

    total = total_memory()
    return DeviceProfile(
        kind="cpu",
        total_bytes=total,
        # On a CPU the memory is not what stops a build - the cores are.
        # The batch is kept modest so the machine stays usable.
        budget_bytes=max(256 * 1024 * 1024, int(total * 0.15)),
        cores=cores,
        note=f"CPU，{cores} 个物理核，内存 {total / 1024 ** 3:.1f} GB",
    )


def physical_cores() -> int:
    """How many physical cores the machine has.

    The logical count is not the right number: the extra threads are
    the hyper-threaded halves of the same cores, and two workers on one
    core do not do twice the work.
    """
    try:
        import psutil

        count = psutil.cpu_count(logical=False)
        if count:
            return int(count)
    except Exception:  # noqa: BLE001 - the fallback is enough
        pass
    import os

    return max(1, os.cpu_count() or 1)


def total_memory() -> int:
    """How much memory the machine has."""
    try:
        import psutil

        return int(psutil.virtual_memory().total)
    except Exception:  # noqa: BLE001 - the fallback is enough
        pass
    return 8 * 1024 ** 3


def cut_batches(
    lengths: list[int],
    order: list[int],
    budget_of: Any,
    max_sequences: int = MAX_SEQUENCES,
    ratio: float = LENGTH_RATIO,
):
    """Cut the slices into batches the device can take.

    Two rules, and both of them matter:

    * **a batch may not be too big.** Its size is counted in padded
      tokens - how many slices times how long the longest of them is -
      because that is what the device actually has to hold.
    * **a batch may not mix a sentence with a page.** It is padded to
      its longest slice, so the slices in a batch are kept within a
      ratio of each other; the ones that are too different start a new
      batch. The ratio is against the **shortest** slice of the batch:
      that is the one every other slice is padded up to.

    The slices are handed over sorted by length, so the batches come
    out with similar slices next to each other and the padding stays
    small.

    The budget is asked for once per batch: a device that has just said
    "that was too much" makes the batches after it smaller.
    """
    current: list[int] = []
    current_min = 0
    current_max = 0
    budget = budget_of()

    for index in order:
        length = max(1, lengths[index])
        new_max = max(current_max, length)
        too_big = (len(current) + 1) * new_max > budget
        too_mixed = current_min and length > current_min * ratio
        if current and (too_big or too_mixed or len(current) >= max_sequences):
            yield current
            current = []
            current_min = 0
            current_max = 0
            new_max = length
            budget = budget_of()
        current.append(index)
        if not current_min:
            current_min = length
        current_max = new_max

    if current:
        yield current


def pack_batches(
    lengths: list[int],
    order: list[int],
    token_budget: int,
    max_sequences: int = MAX_SEQUENCES,
    ratio: float = LENGTH_RATIO,
) -> list[list[int]]:
    """The batches, cut by a budget that does not move."""
    return list(
        cut_batches(
            lengths,
            order,
            lambda: token_budget,
            max_sequences=max_sequences,
            ratio=ratio,
        )
    )


class SlicedEncoder:
    """Encodes slices, cutting the batches by what the device can take."""

    def __init__(
        self,
        embedder: Any,
        device: str,
        max_tokens: int,
        memory_share: float | None = None,
    ):
        self._embedder = embedder
        self._device = (device or "cpu").strip().lower()
        self._max_tokens = max(1, int(max_tokens))
        self._profile = device_profile(self._device)
        if memory_share is not None:
            self._profile = DeviceProfile(
                kind=self._profile.kind,
                total_bytes=self._profile.total_bytes,
                budget_bytes=max(
                    64 * 1024 * 1024,
                    int(self._profile.total_bytes * memory_share),
                ),
                cores=self._profile.cores,
                note=self._profile.note,
            )
        #: How many padded tokens one batch may hold. Replaced by the
        #: measurement as soon as there is one.
        self._token_budget = self._initial_budget()
        #: What one padded token was measured to cost on this device.
        self._bytes_per_token: float | None = None
        #: A cap on how many slices one batch may hold, when the settings
        #: give one. The device may go smaller on its own - never bigger.
        self._sequence_cap: int | None = None
        #: The sizes of the batches this encoder has run - for the log
        #: and for the tests.
        self.batch_sizes: list[int] = []
        #: Whether the model is ready to be run here. When it is not -
        #: an int8 model the device will not take, a library whose
        #: internals are not what this expects - the library's own path
        #: is used instead.
        self._model_ready = self._prepare()

    def _prepare(self) -> bool:
        """Put the model where it is going to run.

        The embedding library moves the model onto the device inside its
        own ``encode()`` - which this path does not go through, so the
        move happens here instead.
        """
        model = getattr(self._embedder, "model", None)
        if model is None:
            return False
        mover = getattr(model, "to", None)
        if mover is None:
            # A stand-in for the model (the tests have one) - there is
            # nothing to move, and nothing that needs moving.
            return True
        try:
            mover(self._device)
            setter = getattr(model, "eval", None)
            if setter is not None:
                setter()
            return True
        except Exception as e:  # noqa: BLE001 - the library's path will do
            logger.warning(
                "模型没能搬到 %s 上，走库自己的路：%r", self._device, e
            )
            return False

    def set_sequence_cap(self, count: int) -> None:
        """The most slices one batch may hold, when the settings say so."""
        if count and count > 0:
            self._sequence_cap = int(count)

    # --------------------------------------------------------------------------
    # the device
    # --------------------------------------------------------------------------

    @property
    def profile(self) -> DeviceProfile:
        return self._profile

    def _initial_budget(self) -> int:
        """The batch size to start with, before anything is measured."""
        if self._profile.kind == "cuda":
            return max(
                MIN_SEQUENCES * 256,
                int(self._profile.budget_bytes // BYTES_PER_TOKEN_GUESS),
            )
        # On a CPU the batch is about keeping the cores busy, not about
        # memory: a bigger batch helps the arithmetic, a smaller one
        # keeps the machine responsive.
        return max(MIN_SEQUENCES, min(64, self._profile.cores * 4)) * 256

    def _measure(self, sequences: int, padded_tokens: int, baseline: int) -> None:
        """Learn what one padded token costs on this device."""
        if self._profile.kind != "cuda":
            return
        try:
            import torch

            used = int(torch.cuda.max_memory_allocated()) - baseline
        except Exception:  # noqa: BLE001 - the guess is good enough
            return
        if padded_tokens <= 0 or used <= 0:
            return
        self._bytes_per_token = used / padded_tokens
        affordable = int(self._profile.budget_bytes / self._bytes_per_token)
        # Leave room: the next batch may hold longer slices than this
        # one did, and the attention matrix grows with the square of
        # that.
        self._token_budget = max(MIN_SEQUENCES * 64, int(affordable * 0.7))

    # --------------------------------------------------------------------------
    # the encoding
    # --------------------------------------------------------------------------

    def can_encode_here(self) -> bool:
        """Whether this encoder knows how to do the work itself."""
        return (
            getattr(self._embedder, "tokenizer", None) is not None
            and getattr(self._embedder, "model", None) is not None
            and self._model_ready
        )

    def encode(self, texts: list[str], cap: int | None = None) -> np.ndarray:
        """Encode the texts, in the order they were given.

        ``cap`` is the most slices one batch may hold, when the caller
        gives one. Without it the device decides.
        """
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        if not self.can_encode_here():
            return self._encode_with_library(texts)

        tokenizer = self._embedder.tokenizer
        encoded = tokenizer(
            list(texts),
            truncation=True,
            max_length=self._max_tokens,
            padding=False,
        )
        id_lists: list[list[int]] = [list(ids) for ids in encoded["input_ids"]]
        lengths = [len(ids) for ids in id_lists]
        order = sorted(range(len(texts)), key=lambda i: lengths[i])

        results: list[Any] = [None] * len(texts)
        limit = cap or self._sequence_cap or MAX_SEQUENCES
        for batch in cut_batches(
            lengths,
            order,
            lambda: self._token_budget,
            max_sequences=limit,
        ):
            vectors = self._forward([id_lists[i] for i in batch])
            for offset, index in enumerate(batch):
                results[index] = vectors[offset]

        return np.asarray(results, dtype=np.float32)

    def _forward(self, id_lists: list[list[int]]) -> np.ndarray:
        """One batch through the model.

        A device that cannot take the batch says so, and the batch is
        cut in half - and the budget is cut with it, so the batches
        after it are smaller too.
        """
        try:
            return self._forward_once(id_lists)
        except OutOfMemory:
            if len(id_lists) <= 1:
                raise
            self._token_budget = max(MIN_SEQUENCES * 64, self._token_budget // 2)
            logger.warning(
                "显存不够，这一批切半（剩下的批次也更小了）：%d → %d",
                len(id_lists), len(id_lists) // 2,
            )
            half = len(id_lists) // 2
            first = self._forward(id_lists[:half])
            second = self._forward(id_lists[half:])
            return np.concatenate([first, second])

    def _forward_once(self, id_lists: list[list[int]]) -> np.ndarray:
        """One batch through the model, once."""
        import torch

        device = self._device
        pad_id = self._embedder.tokenizer.pad_token_id
        width = max((len(ids) for ids in id_lists), default=1)

        input_ids = torch.full((len(id_lists), width), pad_id, dtype=torch.long)
        attention_mask = torch.zeros((len(id_lists), width), dtype=torch.long)
        for row, ids in enumerate(id_lists):
            if ids:
                input_ids[row, : len(ids)] = torch.tensor(ids, dtype=torch.long)
                attention_mask[row, : len(ids)] = 1

        text_input = {
            "input_ids": input_ids.to(device),
            "attention_mask": attention_mask.to(device),
        }

        baseline = 0
        if device == "cuda":
            torch.cuda.synchronize()
            baseline = int(torch.cuda.memory_allocated())
            torch.cuda.reset_peak_memory_stats()

        try:
            # The forward must not build an autograd graph. Without
            # this, every layer keeps its intermediate results alive:
            # measured on bge-m3, **776 KB per token** instead of
            # **22 KB** - 35 times the memory, and a batch that fits
            # in the card turns into one that does not.
            with torch.inference_mode():
                outputs = self._embedder.model(
                    text_input,
                    return_dense=True,
                    return_sparse=False,
                    return_colbert_vecs=False,
                )
        except torch.cuda.OutOfMemoryError as e:
            if device == "cuda":
                torch.cuda.empty_cache()
            raise OutOfMemory(str(e)) from e
        except RuntimeError as e:
            # PyTorch says it in more than one way: "CUDA out of memory",
            # "memory allocation failed with OOM on device 0" — the
            # second one does not contain the words "out of memory", so
            # look for both.
            message = str(e).lower()
            if "out of memory" in message or "memory allocation failed" in message:
                if device == "cuda":
                    torch.cuda.empty_cache()
                raise OutOfMemory(str(e)) from e
            raise
        dense = outputs["dense_vecs"]

        if device == "cuda":
            torch.cuda.synchronize()
            self._measure(len(id_lists), len(id_lists) * width, baseline)
            self.batch_sizes.append(len(id_lists))

        return dense.detach().float().cpu().numpy()

    def _encode_with_library(self, texts: list[str]) -> np.ndarray:
        """The library's own path - the fallback."""
        result = self._embedder.encode(
            texts,
            return_dense=True,
            return_sparse=False,
            return_colbert_vecs=False,
        )
        dense = result["dense_vecs"]
        if hasattr(dense, "tolist"):
            dense = dense.tolist()
        return np.asarray(dense, dtype=np.float32)
