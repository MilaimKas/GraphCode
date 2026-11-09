"""Gaussian noise model.

Covers: inheritance across files, ``super().__init__`` call, a call to a
method inherited from the base class in another file (``self.sample_many``),
a relative import.
"""

from __future__ import annotations

from .base import Noise


class GaussianNoise(Noise):
    """Normal noise with mean zero."""

    def __init__(self, scale: float, seed: int = 0) -> None:
        super().__init__(seed)
        self.scale = scale

    def sample(self) -> float:
        """Draw one normally distributed value."""
        return self.generator.gauss(0.0, self.scale)

    def mean_of(self, count: int) -> float:
        """Average of ``count`` draws."""
        values = self.sample_many(count)
        return sum(values) / count
