"""Base class of noise models.

Covers: a base class whose method is inherited by a subclass in another file,
a method that only raises (``NotImplementedError``).
"""

from __future__ import annotations

import random


class Noise:
    """Base noise model with a seeded random generator."""

    def __init__(self, seed: int = 0) -> None:
        self.generator = random.Random(seed)

    def sample(self) -> float:
        """Draw one value; subclasses must override."""
        raise NotImplementedError

    def sample_many(self, count: int) -> list[float]:
        """Draw ``count`` values (polymorphic call to ``self.sample``)."""
        return [self.sample() for _ in range(count)]
