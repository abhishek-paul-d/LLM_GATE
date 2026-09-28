"""Deterministic release gate: ``evaluate(metrics, policy) -> Decision``.

Pure function. No I/O, no clock, no randomness. Rules run in a fixed order and the
outcome is chosen by this precedence (first match wins):

1. INVALID  - a validity check failed, or a configured limit has no usable metric
2. REJECT   - any safety or hard limit failed (ROLLBACK in canary mode)
3. HOLD     - any limit inconclusive, or a protected slice has too few cases
4. PROMOTE  - every configured limit passed

Evidence rules:
- Absolute limits (``max_*_rate``, ``max_p95_latency_ms``, ...) compare the candidate's
  point estimate. They are service ceilings, not claims about a difference.
- Comparative limits (``max_accuracy_drop``, ``max_*_regression_pct``) are
  non-inferiority checks on the paired CI of (candidate - baseline): pass when the whole
  interval is inside the margin, fail when the whole interval is outside it, and
  inconclusive otherwise. Without a CI they are inconclusive, never a pass.

Every ``RuleResult`` reports ``limit``, ``observed`` and ``ci`` in one unit with an explicit
pass ``direction``, so a report can show them side by side without reinterpretation.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from .models import (
    Category,
    Comparison,
    Decision,
    Outcome,
    Policy,
    RuleResult,
    RunMetrics,
    Status,
)

GATE_VERSION = "0.3.0"  # 0.3.0: protected slices may set min_category_accuracy

# Absorbs float noise such as 0.1 + 0.2 when a value sits exactly on a limit.
_EPS = 1e-9


# --------------------------------------------------------------------------- checks


@dataclass(frozen=True)
class _Eval:
    status: Status
    detail: str
    limit: float
    direction: Literal["<=", ">="]
    observed: float | None
    ci: tuple[float, float] | None
    evidence: Literal["ci", "point_estimate"]


def _absolute_max(c: Comparison, limit: float) -> _Eval:
    ok = c.candidate <= limit + _EPS
    detail = f"candidate {c.candidate:g} {'<=' if ok else '>'} limit {limit:g}"
    return _Eval(Status.PASS if ok else Status.FAIL, detail, limit, "<=", c.candidate, None, "point_estimate")


def _absolute_min(c: Comparison, limit: float) -> _Eval:
    ok = c.candidate >= limit - _EPS
    detail = f"candidate {c.candidate:g} {'>=' if ok else '<'} limit {limit:g}"
    return _Eval(Status.PASS if ok else Status.FAIL, detail, limit, ">=", c.candidate, None, "point_estimate")


def _max_drop(c: Comparison, max_drop: float) -> _Eval:
    """Higher is better. Non-inferiority margin on the paired delta: delta >= -max_drop."""
    floor = -max_drop + 0.0  # normalise -0.0 for display

    def result(status: Status, detail: str) -> _Eval:
        return _Eval(status, detail, floor, ">=", c.delta, c.delta_ci, "ci")

    if c.delta_ci is None:
        return result(Status.INCONCLUSIVE, "no confidence interval for a comparative limit")
    lo, hi = c.delta_ci
    ci = f"delta CI [{lo:+.4g}, {hi:+.4g}]"
    if lo >= floor - _EPS:
        return result(Status.PASS, f"{ci} within allowed drop {max_drop:g}")
    if hi < floor - _EPS:
        return result(Status.FAIL, f"{ci} entirely below allowed drop -{max_drop:g}")
    return result(Status.INCONCLUSIVE, f"{ci} straddles allowed drop -{max_drop:g}")


def _max_regression_pct(c: Comparison, max_pct: float) -> _Eval:
    """Lower is better. Percent change = delta / baseline point value * 100."""
    if c.baseline <= 0:
        detail = f"baseline {c.baseline:g} is not positive; relative change undefined"
        return _Eval(Status.INCONCLUSIVE, detail, max_pct, "<=", None, None, "ci")
    observed = c.delta / c.baseline * 100

    def result(status: Status, detail: str, ci: tuple[float, float] | None = None) -> _Eval:
        return _Eval(status, detail, max_pct, "<=", observed, ci, "ci")

    if c.delta_ci is None:
        return result(Status.INCONCLUSIVE, "no confidence interval for a comparative limit")
    lo, hi = (bound / c.baseline * 100 for bound in c.delta_ci)
    ci = f"relative change CI [{lo:+.1f}%, {hi:+.1f}%]"
    if hi <= max_pct + _EPS:
        return result(Status.PASS, f"{ci} within {max_pct:g}%", (lo, hi))
    if lo > max_pct + _EPS:
        return result(Status.FAIL, f"{ci} entirely above {max_pct:g}%", (lo, hi))
    return result(Status.INCONCLUSIVE, f"{ci} straddles {max_pct:g}%", (lo, hi))


Check = Callable[[Comparison, float], _Eval]


@dataclass(frozen=True)
class _MetricRule:
    rule_id: str
    category: Category
    metric_section: str  # section of RunMetrics
    metric: str
    policy_section: str  # section of Policy
    policy_field: str
    check: Check
    unit: str | None = None


# Order here is the order rules appear in the decision and report.
_METRIC_RULES: tuple[_MetricRule, ...] = (
    _MetricRule(
        "safety.unsafe_command_rate", "safety", "safety", "unsafe_command_rate",
        "safety", "max_unsafe_command_rate", _absolute_max,
    ),
    _MetricRule(
        "safety.injection_compliance_rate", "safety", "safety", "injection_compliance_rate",
        "safety", "max_injection_compliance_rate", _absolute_max,
    ),
    _MetricRule("quality.accuracy_drop", "quality", "quality", "accuracy", "quality", "max_accuracy_drop", _max_drop),
    _MetricRule(
        "quality.unsupported_claim_rate", "quality", "quality", "unsupported_claim_rate",
        "quality", "max_unsupported_claim_rate", _absolute_max,
    ),
    _MetricRule(
        "quality.schema_valid_rate", "quality", "quality", "schema_valid_rate",
        "quality", "min_schema_valid_rate", _absolute_min,
    ),
    _MetricRule(
        "serving.p95_latency_ms", "serving", "serving", "p95_latency_ms",
        "serving", "max_p95_latency_ms", _absolute_max, "ms",
    ),
    _MetricRule(
        "serving.p95_regression", "serving", "serving", "p95_latency_ms",
        "serving", "max_p95_regression_pct", _max_regression_pct, "%",
    ),
    _MetricRule("serving.error_rate", "serving", "serving", "error_rate", "serving", "max_error_rate", _absolute_max),
    _MetricRule("serving.timeout_rate", "serving", "serving", "timeout_rate", "serving", "max_timeout_rate", _absolute_max),
    _MetricRule(
        "cost.cost_per_valid_response", "cost", "cost", "cost_per_valid_response",
        "cost", "max_cost_per_valid_response", _absolute_max, "USD",
    ),
    _MetricRule(
        "cost.cost_regression", "cost", "cost", "cost_per_valid_response",
        "cost", "max_cost_regression_pct", _max_regression_pct, "%",
    ),
)  # fmt: skip


def _apply(
    rule_id: str,
    category: Category,
    metric_path: str,
    policy_field: str,
    comparison: Comparison | None,
    limit: float,
    check: Check,
    unit: str | None,
) -> RuleResult:
    common = dict(rule_id=rule_id, category=category, metric=metric_path, policy_field=policy_field, unit=unit)
    if comparison is None or comparison.n == 0:
        why = "metric not reported" if comparison is None else "metric has zero cases"
        return RuleResult(**common, status=Status.MISSING, message=f"{metric_path}: {why}", limit=limit)
    e = check(comparison, limit)
    return RuleResult(
        **common,
        status=e.status,
        message=f"{metric_path}: {e.detail}",
        limit=e.limit,
        direction=e.direction,
        observed=e.observed,
        ci=e.ci,
        n=comparison.n,
        evidence=e.evidence,
    )


# --------------------------------------------------------------------------- rule groups


def _validity_rules(m: RunMetrics, p: Policy) -> Iterator[RuleResult]:
    v = m.validity

    def check(rule_id: str, ok: bool, message: str, **extra: object) -> RuleResult:
        return RuleResult(
            rule_id=rule_id,
            category="validity",
            status=Status.PASS if ok else Status.FAIL,
            message=message,
            evidence="check",
            **extra,
        )

    yield check(
        "validity.manifest",
        not v.manifest_mismatches,
        "run manifest consistent" if not v.manifest_mismatches else "manifest mismatch: " + "; ".join(v.manifest_mismatches),
    )
    yield check(
        "validity.baseline_healthy",
        v.baseline_healthy,
        "baseline endpoint healthy" if v.baseline_healthy else "baseline endpoint unhealthy",
    )
    limit = p.validity.max_infra_error_rate
    yield check(
        "validity.infra_error_rate",
        v.infra_error_rate <= limit + _EPS,
        f"infrastructure error rate {v.infra_error_rate:g} (limit {limit:g})",
        policy_field="validity.max_infra_error_rate",
        limit=limit,
        direction="<=",
        observed=v.infra_error_rate,
    )
    tol = p.validity.baseline_replay_tolerance
    if v.baseline_replay_max_deviation is None:
        yield RuleResult(
            rule_id="validity.baseline_replay",
            category="validity",
            status=Status.SKIPPED,
            message="no approved baseline record to replay against",
            policy_field="validity.baseline_replay_tolerance",
            limit=tol,
            direction="<=",
            evidence="check",
        )
    else:
        dev = v.baseline_replay_max_deviation
        yield check(
            "validity.baseline_replay",
            dev <= tol + _EPS,
            f"baseline replay max deviation {dev:g} (tolerance {tol:g})",
            policy_field="validity.baseline_replay_tolerance",
            limit=tol,
            direction="<=",
            observed=dev,
        )
    want, got = p.decision.confidence_level, m.stats.confidence_level
    yield check(
        "validity.confidence_level",
        math.isclose(want, got),
        f"metrics computed at confidence {got:g}; policy requires {want:g}"
        + ("" if math.isclose(want, got) else " (recompute stats before gating)"),
        policy_field="decision.confidence_level",
        limit=want,
        direction="==",
        observed=got,
    )


def _metric_rules(m: RunMetrics, p: Policy) -> Iterator[RuleResult]:
    for r in _METRIC_RULES:
        section = getattr(p, r.policy_section)
        limit = None if section is None else getattr(section, r.policy_field)
        if limit is None:
            continue  # limit not configured in this policy
        comparison = getattr(m, r.metric_section).get(r.metric)
        yield _apply(
            r.rule_id,
            r.category,
            f"{r.metric_section}.{r.metric}",
            f"{r.policy_section}.{r.policy_field}",
            comparison,
            limit,
            r.check,
            r.unit,
        )


# (rule suffix, SliceLimits field, slice metric, check), in the order rules appear per slice.
_SLICE_RULES: tuple[tuple[str, str, str, Check], ...] = (
    ("accuracy_drop", "max_accuracy_drop", "accuracy", _max_drop),
    ("min_category_accuracy", "min_category_accuracy", "category_accuracy", _absolute_min),
)


def _slice_rules(m: RunMetrics, p: Policy) -> Iterator[RuleResult]:
    minimum = p.decision.minimum_cases_per_slice
    for name in sorted(p.protected_slices):
        limits = p.protected_slices[name]
        for suffix, field, metric, check in _SLICE_RULES:
            limit = getattr(limits, field)
            if limit is None:
                continue
            rule_id = f"slice.{name}.{suffix}"
            metric_path = f"slices.{name}.{metric}"
            policy_field = f"protected_slices.{name}.{field}"
            comparison = m.slices.get(name, {}).get(metric)
            if comparison is not None and 0 < comparison.n < minimum:
                e = check(comparison, limit)  # for the reported values; too few cases to judge
                yield RuleResult(
                    rule_id=rule_id,
                    category="slice",
                    status=Status.INSUFFICIENT,
                    message=f"{metric_path}: {comparison.n} cases, protected slices need at least {minimum}",
                    metric=metric_path,
                    policy_field=policy_field,
                    limit=e.limit,
                    direction=e.direction,
                    observed=e.observed,
                    ci=e.ci,
                    n=comparison.n,
                    evidence=e.evidence,
                )
                continue
            yield _apply(rule_id, "slice", metric_path, policy_field, comparison, limit, check, None)


# --------------------------------------------------------------------------- decision


def _decide(results: list[RuleResult], mode: str) -> tuple[Outcome, list[RuleResult]]:
    invalid = [r for r in results if (r.category == "validity" and r.status == Status.FAIL) or r.status == Status.MISSING]
    if invalid:
        return Outcome.INVALID, invalid
    failed = [r for r in results if r.status == Status.FAIL]
    if failed:
        return (Outcome.ROLLBACK if mode == "canary" else Outcome.REJECT), failed
    unsure = [r for r in results if r.status in (Status.INCONCLUSIVE, Status.INSUFFICIENT)]
    if unsure:
        return Outcome.HOLD, unsure
    return Outcome.PROMOTE, []


_REASON_PREFIX = {
    Outcome.INVALID: "Run is not valid evidence; fix the run before gating",
    Outcome.REJECT: "Candidate failed release limits",
    Outcome.ROLLBACK: "Canary candidate failed release limits; recommend rollback",
    Outcome.HOLD: "Evidence is inconclusive; more data or review needed",
    Outcome.PROMOTE: "All configured release limits passed",
}


def evaluate(metrics: RunMetrics, policy: Policy) -> Decision:
    results = [*_validity_rules(metrics, policy), *_metric_rules(metrics, policy), *_slice_rules(metrics, policy)]
    outcome, triggered = _decide(results, metrics.mode)
    reason = _REASON_PREFIX[outcome]
    if triggered:
        reason += " (" + "; ".join(r.message for r in triggered) + ")"
    return Decision(
        gate_version=GATE_VERSION,
        run_id=metrics.run_id,
        policy_version=policy.policy_version,
        mode=metrics.mode,
        outcome=outcome,
        reason=reason,
        triggered_rules=[r.rule_id for r in triggered],
        rules=results,
    )
