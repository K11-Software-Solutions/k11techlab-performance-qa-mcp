"""Latency statistics and the significance test behind regression calls.

Percentiles use linear interpolation (the same definition as numpy's default). A slowdown only
counts as a regression when it is bigger than the tolerance, bigger than a minimum number of
milliseconds, and statistically significant (one-sided Mann-Whitney U test, normal approximation
with tie correction), so ordinary run-to-run noise doesn't block a release.
"""

from __future__ import annotations

import math
import random
from typing import Sequence


def percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * q / 100
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo))


def summarize(values: Sequence[float]) -> dict:
    s = sorted(values)
    if not s:
        return {"min": 0, "avg": 0, "p50": 0, "p90": 0, "p95": 0, "p99": 0, "max": 0}
    r = lambda x: round(x, 1)  # noqa: E731
    return {"min": r(s[0]), "avg": r(sum(s) / len(s)), "p50": r(percentile(s, 50)), "p90": r(percentile(s, 90)),
            "p95": r(percentile(s, 95)), "p99": r(percentile(s, 99)), "max": r(s[-1])}


def downsample(values: Sequence[float], k: int = 2000, seed: int = 11) -> list[float]:
    """A reproducible random sample that keeps the distribution (for baselines)."""
    if len(values) <= k:
        return [round(v, 2) for v in values]
    rng = random.Random(seed)
    return [round(v, 2) for v in rng.sample(list(values), k)]


def _phi(z: float) -> float:
    return 0.5 * math.erfc(-z / math.sqrt(2))


def mann_whitney_greater(baseline: Sequence[float], current: Sequence[float]) -> float:
    """p-value for 'current tends to be slower than baseline' (one-sided). 1.0 when it can't be computed."""
    n1, n2 = len(baseline), len(current)
    if n1 < 2 or n2 < 2:
        return 1.0
    combined = sorted([(v, 0) for v in baseline] + [(v, 1) for v in current], key=lambda x: x[0])
    ranks = [0.0] * len(combined)
    ties = 0.0
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[j + 1][0] == combined[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[k] = avg
        t = j - i + 1
        ties += t ** 3 - t
        i = j + 1
    r2 = sum(r for r, (_, g) in zip(ranks, combined) if g == 1)
    u2 = r2 - n2 * (n2 + 1) / 2
    n = n1 + n2
    var = n1 * n2 / 12 * ((n + 1) - ties / (n * (n - 1)))
    if var <= 0:
        return 1.0
    z = (u2 - n1 * n2 / 2 - 0.5) / math.sqrt(var)
    return round(1 - _phi(z), 6)
