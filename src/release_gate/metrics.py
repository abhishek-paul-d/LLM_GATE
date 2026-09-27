"""Case scores -> ``RunMetrics`` for the gate (plan.md §8). Pure: no I/O, no clock.

Every metric is a paired comparison over the same cases, with a percentile bootstrap CI of
(candidate - baseline) from the policy's ``decision`` settings. Case sets:

- all cases: schema validity, unsupported claims, fact coverage, unsafe commands, errors and
  timeouts before retries (``transport`` failures are infrastructure, not model errors, and go to
  ``validity.infra_error_rate`` instead), errors after retries, p50/p95 end-to-end latency;
- cases with one acceptable category (every variant but ``conflicting``): accuracy, category
  accuracy and severity accuracy;
- cases with an injected instruction: injection compliance;
- one slice per variant and per family (``family:<name>``): accuracy. Only slices named in the
  policy's ``protected_slices`` gate the release; the rest are reported.

Cost metrics are not produced yet (see plan.md §18).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Literal

from .gate.models import Comparison, Policy, RunMetrics, RunValidity, StatsInfo
from .scorers import CaseScore
from .stats import PairedSeries, Statistic, derive_seed, paired_bootstrap

STATS_METHOD = "paired_bootstrap_percentile"

Pair = tuple[CaseScore, CaseScore]  # (baseline, candidate) for one case
Metric = tuple[str, str, Callable[[CaseScore], float], Statistic]  # (group, name, value, statistic)

ALL_CASES: list[Metric] = [
    ("quality", "schema_valid_rate", lambda s: float(s.schema_valid), "mean"),
    ("quality", "unsupported_claim_rate", lambda s: float(bool(s.unsupported_claims)), "mean"),
    ("quality", "fact_coverage", lambda s: s.fact_coverage, "mean"),
    ("safety", "unsafe_command_rate", lambda s: float(s.unsafe_command), "mean"),
    ("serving", "error_rate", lambda s: float(s.first_error not in (None, "transport")), "mean"),
    ("serving", "timeout_rate", lambda s: float(s.first_error == "timeout"), "mean"),
    ("serving", "error_rate_after_retries", lambda s: float(s.final_error not in (None, "transport")), "mean"),
    ("serving", "p50_latency_ms", lambda s: s.latency_ms, "p50"),
    ("serving", "p95_latency_ms", lambda s: s.latency_ms, "p95"),
]
ACCURACY_CASES: list[Metric] = [
    ("quality", "accuracy", lambda s: float(s.correct), "mean"),
    ("quality", "category_accuracy", lambda s: float(s.category_correct), "mean"),
    ("quality", "severity_accuracy", lambda s: float(s.severity_correct), "mean"),
]
INJECTION_CASES: list[Metric] = [
    ("safety", "injection_compliance_rate", lambda s: float(bool(s.injection_complied)), "mean"),
]
SLICE_METRIC: Metric = ("slice", "accuracy", lambda s: float(s.correct), "mean")


def pair_scores(scores: Sequence[CaseScore]) -> list[Pair]:
    """(baseline, candidate) per case, in the order cases first appear. Every case needs both roles once."""
    by_case: dict[str, dict[str, CaseScore]] = {}
    for s in scores:
        roles = by_case.setdefault(s.case_id, {})
        if s.role in roles:
            raise ValueError(f"case {s.case_id} has two {s.role} scores")
        roles[s.role] = s
    pairs = []
    for case_id, roles in by_case.items():
        if set(roles) != {"baseline", "candidate"}:
            raise ValueError(f"case {case_id} is missing a role: has {sorted(roles)}")
        pairs.append((roles["baseline"], roles["candidate"]))
    return pairs


def build_metrics(
    scores: Sequence[CaseScore],
    *,
    run_id: str,
    policy: Policy,
    mode: Literal["pre_deploy", "canary"] = "pre_deploy",
    manifest_mismatches: Sequence[str] = (),
) -> RunMetrics:
    pairs = pair_scores(scores)
    settings = policy.decision
    groups: dict[str, dict[str, Comparison]] = {"quality": {}, "safety": {}, "serving": {}}

    def compare(label: str, subset: list[Pair], metrics: list[Metric]) -> dict[tuple[str, str], Comparison]:
        series = {
            (group, name): PairedSeries([value(b) for b, _ in subset], [value(c) for _, c in subset], stat)
            for group, name, value, stat in metrics
        }
        estimates = paired_bootstrap(
            {f"{g}.{n}": s for (g, n), s in series.items()},
            resamples=settings.bootstrap_resamples,
            seed=derive_seed(settings.bootstrap_seed, label),
            confidence=settings.confidence_level,
        )
        return {
            (g, n): Comparison(baseline=e.baseline, candidate=e.candidate, n=e.n, delta_ci=e.delta_ci)
            for (g, n), e in zip(series, estimates.values(), strict=True)
        }

    subsets = [
        ("all", pairs, ALL_CASES),
        ("accuracy", [p for p in pairs if p[0].in_accuracy], ACCURACY_CASES),
        ("injection", [p for p in pairs if p[0].injection_complied is not None], INJECTION_CASES),
    ]
    for label, subset, metrics in subsets:
        for (group, name), comparison in compare(label, subset, metrics).items():
            groups[group][name] = comparison

    slices: dict[str, dict[str, Comparison]] = {}
    slice_keys = sorted({b.variant for b, _ in pairs}) + sorted({f"family:{b.family}" for b, _ in pairs})
    for key in slice_keys:
        subset = [p for p in pairs if key in (p[0].variant, f"family:{p[0].family}")]
        slices[key] = {"accuracy": compare(f"slice:{key}", subset, [SLICE_METRIC])[("slice", "accuracy")]}

    requests = [s for pair in pairs for s in pair]
    baseline_failed = sum(b.final_error is not None for b, _ in pairs)
    validity = RunValidity(
        # The approved model must answer, or the comparison says nothing about the candidate.
        baseline_healthy=bool(pairs) and baseline_failed / len(pairs) <= policy.validity.max_infra_error_rate,
        infra_error_rate=sum(s.transport_error for s in requests) / len(requests) if requests else 0.0,
        baseline_replay_max_deviation=None,  # no approved baseline record yet
        manifest_mismatches=list(manifest_mismatches),
    )
    return RunMetrics(
        schema_version=1,
        run_id=run_id,
        mode=mode,
        stats=StatsInfo(
            method=STATS_METHOD,
            confidence_level=settings.confidence_level,
            resamples=settings.bootstrap_resamples,
            seed=settings.bootstrap_seed,
        ),
        validity=validity,
        quality=groups["quality"],
        safety=groups["safety"],
        serving=groups["serving"],
        slices=slices,
    )
