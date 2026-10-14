from __future__ import annotations

import pytest

from evidence_judge.bench import Proportion, wilson_interval


@pytest.mark.parametrize(
    ("k", "n", "low", "high"),
    [
        (81, 263, 0.2553, 0.3662),
        (15, 148, 0.0624, 0.1605),
        (0, 20, 0.0, 0.1611),
        (1, 29, 0.0061, 0.1718),
        (5, 10, 0.2366, 0.7634),
        (0, 10, 0.0, 0.2775),
        (10, 10, 0.7225, 1.0),
    ],
)
def test_wilson_matches_published_values(k: int, n: int, low: float, high: float) -> None:
    lo, hi = wilson_interval(k, n)
    assert lo == pytest.approx(low, abs=5e-5)
    assert hi == pytest.approx(high, abs=5e-5)


def test_wilson_bounds_are_exact_at_the_extremes() -> None:
    assert wilson_interval(0, 60)[0] == 0.0
    assert wilson_interval(60, 60)[1] == 1.0


def test_wilson_with_no_trials_is_uninformative() -> None:
    assert wilson_interval(0, 0) == (0.0, 1.0)


@pytest.mark.parametrize(("k", "n"), [(-1, 5), (6, 5), (0, -1)])
def test_wilson_rejects_impossible_counts(k: int, n: int) -> None:
    with pytest.raises(ValueError, match="successes"):
        wilson_interval(k, n)


def test_wider_z_gives_a_wider_interval() -> None:
    lo95, hi95 = wilson_interval(3, 10)
    lo99, hi99 = wilson_interval(3, 10, z=2.5758293035489)
    assert lo99 < lo95 < 0.3 < hi95 < hi99


def test_proportion_carries_rate_and_interval() -> None:
    p = Proportion.of(3, 12)
    assert (p.k, p.n, p.rate) == (3, 12, 0.25)
    assert (p.ci_low, p.ci_high) == wilson_interval(3, 12)
    assert Proportion.of(0, 0).rate is None
