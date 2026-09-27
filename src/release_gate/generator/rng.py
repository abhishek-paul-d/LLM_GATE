"""Deterministic randomness for suite generation.

Python only guarantees that seeding and ``Random.random()`` stay stable across versions;
``choice``, ``randint``, ``shuffle`` and friends have changed before. Every helper here is
built on ``random()`` alone so the same seed yields a byte-identical suite on any Python.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence
from typing import TypeVar

T = TypeVar("T")


def derive_seed(*parts: object) -> int:
    """Stable 63-bit seed from any parts (suite seed, template id, variant, index...)."""
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") >> 1


class Rng:
    def __init__(self, seed: int) -> None:
        self._r = random.Random(seed)

    def unit(self) -> float:
        return self._r.random()

    def below(self, n: int) -> int:
        """Integer in [0, n)."""
        if n <= 0:
            raise ValueError("n must be positive")
        return min(int(self._r.random() * n), n - 1)

    def between(self, lo: int, hi: int) -> int:
        """Integer in [lo, hi], inclusive."""
        return lo + self.below(hi - lo + 1)

    def uniform(self, lo: float, hi: float) -> float:
        return lo + (hi - lo) * self._r.random()

    def pick(self, seq: Sequence[T]) -> T:
        return seq[self.below(len(seq))]

    def sample(self, seq: Sequence[T], k: int) -> list[T]:
        """k distinct items in a deterministic order."""
        pool = list(seq)
        if k > len(pool):
            raise ValueError(f"cannot sample {k} from {len(pool)}")
        return [pool.pop(self.below(len(pool))) for _ in range(k)]

    def hex(self, n: int) -> str:
        return "".join("0123456789abcdef"[self.below(16)] for _ in range(n))
