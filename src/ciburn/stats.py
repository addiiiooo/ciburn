"""Tiny statistics helpers (no numpy at runtime)."""

from __future__ import annotations

import math
from collections.abc import Sequence


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile; ``p`` in [0, 100]. Empty input -> 0.0."""
    if not values:
        return 0.0
    xs = sorted(values)
    k = max(1, math.ceil(p / 100.0 * len(xs)))
    return float(xs[min(k, len(xs)) - 1])


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    return float(xs[mid]) if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def confidence_from_count(n: int, high: int = 30, medium: int = 10) -> str:
    return "high" if n >= high else "medium" if n >= medium else "low"
