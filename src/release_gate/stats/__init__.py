"""Deterministic paired statistics: bootstrap confidence intervals and McNemar."""

from .paired import (
    McNemar,
    PairedEstimate,
    PairedSeries,
    Statistic,
    derive_seed,
    mcnemar_exact,
    paired_bootstrap,
    quantile,
    statistic,
)

__all__ = [
    "Statistic",
    "McNemar",
    "PairedEstimate",
    "PairedSeries",
    "derive_seed",
    "mcnemar_exact",
    "paired_bootstrap",
    "quantile",
    "statistic",
]
