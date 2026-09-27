"""Paired statistics for candidate vs baseline on the same cases (plan.md §8 Statistical method).

- ``paired_bootstrap``: percentile bootstrap confidence intervals of (candidate - baseline) for
  several metrics at once. Each resample draws case indices with replacement and applies them
  to **both** roles, so the pairing is kept. All metrics over one case set share the same
  resamples, which is both faster and consistent.
- ``mcnemar_exact``: exact McNemar test on paired binary outcomes (reported, not gated).

Deterministic: the only randomness is ``random.Random(int).random()``, whose output for an
integer seed is stable across Python versions, seeded from the policy's ``bootstrap_seed`` and a
label for the case set (``derive_seed``). Pure: no I/O, no clock.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

Statistic = Literal["mean", "p50", "p95", "p99"]
_QUANTILES = {"p50": 0.50, "p95": 0.95, "p99": 0.99}


@dataclass(frozen=True)
class PairedSeries:
    baseline: Sequence[float]
    candidate: Sequence[float]
    statistic: Statistic = "mean"

    def __post_init__(self) -> None:
        if len(self.baseline) != len(self.candidate):
            raise ValueError(f"paired series differ in length: {len(self.baseline)} vs {len(self.candidate)}")
        if self.statistic != "mean" and self.statistic not in _QUANTILES:
            raise ValueError(f"unknown statistic {self.statistic!r}")


@dataclass(frozen=True)
class PairedEstimate:
    baseline: float
    candidate: float
    n: int
    delta_ci: tuple[float, float] | None  # None when n == 0


def derive_seed(seed: int, label: str) -> int:
    """Independent, reproducible seed for one case set (e.g. "all", "slice:missing_evidence")."""
    digest = hashlib.sha256(f"{seed}\x1f{label}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear interpolation between closest ranks (numpy's default method)."""
    if not sorted_values:
        raise ValueError("quantile of an empty sequence")
    pos = q * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def statistic(values: Sequence[float], kind: Statistic) -> float:
    if kind == "mean":
        return math.fsum(values) / len(values)
    return quantile(sorted(values), _QUANTILES[kind])


def paired_bootstrap(
    series: Mapping[str, PairedSeries],
    *,
    resamples: int,
    seed: int,
    confidence: float,
) -> dict[str, PairedEstimate]:
    """Point estimates and percentile CIs of (candidate - baseline) for every series.

    All series must cover the same n cases in the same order. The CI is ``None`` when n == 0.
    """
    lengths = {len(s.baseline) for s in series.values()}
    if len(lengths) > 1:
        raise ValueError(f"series cover different case sets: lengths {sorted(lengths)}")
    if not 0.0 < confidence < 1.0:
        raise ValueError("confidence must be between 0 and 1")
    if resamples < 1:
        raise ValueError("resamples must be >= 1")
    n = lengths.pop() if lengths else 0
    if n == 0:
        return {name: PairedEstimate(0.0, 0.0, 0, None) for name in series}

    rng = random.Random(seed)
    deltas: dict[str, list[float]] = {name: [] for name in series}
    mean_diffs = {
        name: [c - b for b, c in zip(s.baseline, s.candidate, strict=True)] for name, s in series.items() if s.statistic == "mean"
    }
    for _ in range(resamples):
        idx = [int(rng.random() * n) for _ in range(n)]
        for name, s in series.items():
            if s.statistic == "mean":
                d = mean_diffs[name]
                deltas[name].append(math.fsum(d[i] for i in idx) / n)
            else:
                b = statistic([s.baseline[i] for i in idx], s.statistic)
                c = statistic([s.candidate[i] for i in idx], s.statistic)
                deltas[name].append(c - b)

    alpha = 1.0 - confidence
    out = {}
    for name, s in series.items():
        dist = sorted(deltas[name])
        ci = (_clean(quantile(dist, alpha / 2)), _clean(quantile(dist, 1 - alpha / 2)))
        out[name] = PairedEstimate(
            baseline=_clean(statistic(s.baseline, s.statistic)),
            candidate=_clean(statistic(s.candidate, s.statistic)),
            n=n,
            delta_ci=ci,
        )
    return out


@dataclass(frozen=True)
class McNemar:
    baseline_only: int  # cases the baseline got right and the candidate got wrong
    candidate_only: int  # cases the candidate got right and the baseline got wrong
    p_value: float


def mcnemar_exact(baseline: Sequence[bool], candidate: Sequence[bool]) -> McNemar:
    """Two-sided exact McNemar test (binomial on discordant pairs, p = 0.5)."""
    if len(baseline) != len(candidate):
        raise ValueError("paired outcomes differ in length")
    b = sum(1 for x, y in zip(baseline, candidate, strict=True) if x and not y)
    c = sum(1 for x, y in zip(baseline, candidate, strict=True) if y and not x)
    discordant = b + c
    if discordant == 0:
        return McNemar(b, c, 1.0)
    tail = sum(math.comb(discordant, k) for k in range(min(b, c) + 1))
    return McNemar(b, c, min(1.0, 2 * tail / 2**discordant))


def _clean(x: float) -> float:
    """Round away float noise (e.g. 0.30000000000000004) so saved metrics are stable and readable."""
    return round(x, 12) + 0.0  # + 0.0 turns -0.0 into 0.0
