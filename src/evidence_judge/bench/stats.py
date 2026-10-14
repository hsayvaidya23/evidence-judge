"""Binomial proportion intervals for small benchmark samples."""

from __future__ import annotations

import math
from typing import Final

Z_95: Final = 1.959963984540054
"""Two-sided 95% standard normal quantile."""


def wilson_interval(successes: int, n: int, *, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion.

    Unlike the normal (Wald) interval it stays inside [0, 1] and is informative at 0/n and n/n,
    which is exactly where a robust judge's false-PASS rate lands. With no trials the
    proportion is unknown and the interval is [0, 1].
    """
    if n < 0 or successes < 0 or successes > n:
        raise ValueError(f"need 0 <= successes <= n, got {successes}/{n}")
    if n == 0:
        return 0.0, 1.0
    p = successes / n
    z2 = z * z
    denominator = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denominator
    low = 0.0 if successes == 0 else max(0.0, centre - half)
    high = 1.0 if successes == n else min(1.0, centre + half)
    return low, high
