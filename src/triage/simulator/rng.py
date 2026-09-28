"""Deterministic random numbers, stable across Python versions and platforms.

CPython guarantees only that ``random.Random.random()`` yields the same sequence for the
same seed across versions; ``randint``, ``choice`` and ``gauss`` may change. Every helper
here is built from ``random()`` plus basic arithmetic, which IEEE 754 rounds exactly, so the
same seed gives byte-identical fixtures on any machine. No ``math.sin``/``log``: libm results
are not guaranteed to be bit-identical between platforms.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def derive_seed(*parts: object) -> int:
    """Stable 64-bit seed from arbitrary labels (unlike ``hash()``, not salted per process)."""
    digest = hashlib.sha256(":".join(str(part) for part in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


class Rng:
    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._random = random.Random(seed)

    def derive(self, *labels: object) -> Rng:
        """Independent sub-stream: consuming numbers in one component never shifts another."""
        return Rng(derive_seed(self.seed, *labels))

    def random(self) -> float:
        return self._random.random()

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self._random.random()

    def randint(self, low: int, high: int) -> int:
        """Integer in [low, high], both inclusive."""
        span = high - low + 1
        return low + min(int(self._random.random() * span), span - 1)

    def choice(self, items: Sequence[T]) -> T:
        if not items:
            raise ValueError("choice() from an empty sequence")
        return items[self.randint(0, len(items) - 1)]

    def chance(self, probability: float) -> bool:
        return self._random.random() < probability

    def jitter(self, value: float, relative: float) -> float:
        return value * (1 + self.uniform(-relative, relative))

    def bell(self, low: float, high: float) -> float:
        """Bell-shaped value in [low, high]: mean of three uniforms, arithmetic only."""
        mean = (self._random.random() + self._random.random() + self._random.random()) / 3
        return low + (high - low) * mean

    def token(self, length: int, alphabet: str) -> str:
        return "".join(self.choice(alphabet) for _ in range(length))
