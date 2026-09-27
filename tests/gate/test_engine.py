"""Gate engine behaviour: every outcome, the precedence order, and the evidence rules.

Each case starts from ``pass_metrics.json`` (a PROMOTE run) and applies the smallest
change that should produce the expected outcome.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from release_gate.gate import (
    EXIT_CODES,
    Outcome,
    Policy,
    RunMetrics,
    Status,
    evaluate,
    load_policy,
)

ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = ROOT / "policies" / "policy_v1.yaml"
PASS_METRICS = ROOT / "tests" / "fixtures" / "gate" / "pass_metrics.json"

_DELETE = object()


def _set(doc: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    for k in keys[:-1]:
        doc = doc[k]
    if value is _DELETE:
        del doc[keys[-1]]
    else:
        doc[keys[-1]] = value


def _metrics(**patches: Any) -> RunMetrics:
    doc = json.loads(PASS_METRICS.read_text(encoding="utf-8"))
    for dotted, value in patches.items():
        _set(doc, dotted.replace("__", "."), value)
    return RunMetrics.model_validate(doc)


def _policy(**patches: Any) -> Policy:
    import yaml

    doc = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    for dotted, value in patches.items():
        _set(doc, dotted.replace("__", "."), value)
    return Policy.model_validate(doc)


def _rule(decision, rule_id):
    return next(r for r in decision.rules if r.rule_id == rule_id)


# --------------------------------------------------------------------------- outcome table

CASES: list[tuple[str, dict[str, Any], Outcome, list[str]]] = [
    ("baseline pass", {}, Outcome.PROMOTE, []),
    # quality
    (
        "confident accuracy regression",
        {"quality__accuracy__candidate": 0.81, "quality__accuracy__delta_ci": [-0.08, -0.03]},
        Outcome.REJECT,
        ["quality.accuracy_drop"],
    ),
    (
        "accuracy CI straddles margin",
        {"quality__accuracy__candidate": 0.85, "quality__accuracy__delta_ci": [-0.05, 0.01]},
        Outcome.HOLD,
        ["quality.accuracy_drop"],
    ),
    (
        "unsupported claims above ceiling",
        {"quality__unsupported_claim_rate__candidate": 0.05},
        Outcome.REJECT,
        ["quality.unsupported_claim_rate"],
    ),
    (
        "schema validity below floor",
        {"quality__schema_valid_rate__candidate": 0.97},
        Outcome.REJECT,
        ["quality.schema_valid_rate"],
    ),
    # serving: plan.md §6 example - latency doubles but stays under the absolute ceiling
    (
        "p95 doubles under absolute ceiling",
        {
            "serving__p95_latency_ms__baseline": 1500,
            "serving__p95_latency_ms__candidate": 3000,
            "serving__p95_latency_ms__delta_ci": [1300, 1700],
        },
        Outcome.REJECT,
        ["serving.p95_regression"],
    ),
    (
        "p95 over absolute ceiling",
        {
            "serving__p95_latency_ms__baseline": 3900,
            "serving__p95_latency_ms__candidate": 4200,
            "serving__p95_latency_ms__delta_ci": [100, 500],
        },
        Outcome.REJECT,
        ["serving.p95_latency_ms"],
    ),
    (
        "p95 regression CI straddles margin",
        {"serving__p95_latency_ms__candidate": 2200, "serving__p95_latency_ms__delta_ci": [200, 600]},
        Outcome.HOLD,
        ["serving.p95_regression"],
    ),
    ("error rate over ceiling", {"serving__error_rate__candidate": 0.03}, Outcome.REJECT, ["serving.error_rate"]),
    ("timeout rate over ceiling", {"serving__timeout_rate__candidate": 0.02}, Outcome.REJECT, ["serving.timeout_rate"]),
    # cost
    (
        "cost regression",
        {"cost__cost_per_valid_response__candidate": 0.006, "cost__cost_per_valid_response__delta_ci": [0.0015, 0.0025]},
        Outcome.REJECT,
        ["cost.cost_regression"],
    ),
    # safety: zero tolerance on point estimate
    ("one unsafe command", {"safety__unsafe_command_rate__candidate": 1 / 300}, Outcome.REJECT, ["safety.unsafe_command_rate"]),
    (
        "followed an injection",
        {"safety__injection_compliance_rate__candidate": 1 / 30},
        Outcome.REJECT,
        ["safety.injection_compliance_rate"],
    ),
    # slices
    (
        "protected slice regression",
        {"slices__missing_evidence__accuracy__candidate": 0.5, "slices__missing_evidence__accuracy__delta_ci": [-0.33, -0.1]},
        Outcome.REJECT,
        ["slice.missing_evidence.accuracy_drop"],
    ),
    (
        "protected slice too small",
        {"slices__prompt_injection__accuracy__n": 12},
        Outcome.HOLD,
        ["slice.prompt_injection.accuracy_drop"],
    ),
    (
        "unprotected slice regression is reported, not gated",
        {"slices__long_input__accuracy__candidate": 0.5, "slices__long_input__accuracy__delta_ci": [-0.5, -0.2]},
        Outcome.PROMOTE,
        [],
    ),
    # evidence
    ("comparative limit without CI", {"quality__accuracy__delta_ci": None}, Outcome.HOLD, ["quality.accuracy_drop"]),
    ("metric missing", {"quality__accuracy": _DELETE}, Outcome.INVALID, ["quality.accuracy_drop"]),
    ("metric with zero cases", {"serving__error_rate__n": 0}, Outcome.INVALID, ["serving.error_rate"]),
    ("protected slice absent", {"slices__prompt_injection": _DELETE}, Outcome.INVALID, ["slice.prompt_injection.accuracy_drop"]),
    # validity
    ("baseline unhealthy", {"validity__baseline_healthy": False}, Outcome.INVALID, ["validity.baseline_healthy"]),
    ("infra errors", {"validity__infra_error_rate": 0.2}, Outcome.INVALID, ["validity.infra_error_rate"]),
    ("manifest mismatch", {"validity__manifest_mismatches": ["suite hash differs"]}, Outcome.INVALID, ["validity.manifest"]),
    ("baseline replay drift", {"validity__baseline_replay_max_deviation": 0.05}, Outcome.INVALID, ["validity.baseline_replay"]),
    ("stats at wrong confidence", {"stats__confidence_level": 0.9}, Outcome.INVALID, ["validity.confidence_level"]),
    ("no approved baseline yet", {"validity__baseline_replay_max_deviation": None}, Outcome.PROMOTE, []),
    # mode
    (
        "canary failure rolls back",
        {"mode": "canary", "serving__error_rate__candidate": 0.03},
        Outcome.ROLLBACK,
        ["serving.error_rate"],
    ),
    ("canary pass promotes", {"mode": "canary"}, Outcome.PROMOTE, []),
]


@pytest.mark.parametrize(("name", "patches", "outcome", "triggered"), CASES, ids=[c[0] for c in CASES])
def test_outcomes(name, patches, outcome, triggered):
    decision = evaluate(_metrics(**patches), load_policy(POLICY_PATH))
    assert decision.outcome == outcome, decision.reason
    assert decision.triggered_rules == triggered


# --------------------------------------------------------------------------- precedence


def test_invalid_beats_reject():
    d = evaluate(_metrics(validity__baseline_healthy=False, serving__error_rate__candidate=0.5), load_policy(POLICY_PATH))
    assert d.outcome == Outcome.INVALID
    assert d.triggered_rules == ["validity.baseline_healthy"]
    assert _rule(d, "serving.error_rate").status == Status.FAIL  # still evaluated and reported


def test_reject_beats_hold():
    d = evaluate(
        _metrics(serving__error_rate__candidate=0.5, slices__prompt_injection__accuracy__n=5),
        load_policy(POLICY_PATH),
    )
    assert d.outcome == Outcome.REJECT
    assert d.triggered_rules == ["serving.error_rate"]


def test_missing_metric_beats_failure():
    d = evaluate(_metrics(quality__accuracy=_DELETE, serving__error_rate__candidate=0.5), load_policy(POLICY_PATH))
    assert d.outcome == Outcome.INVALID


def test_all_failures_listed():
    d = evaluate(
        _metrics(serving__error_rate__candidate=0.5, safety__unsafe_command_rate__candidate=0.1),
        load_policy(POLICY_PATH),
    )
    assert d.triggered_rules == ["safety.unsafe_command_rate", "serving.error_rate"]


# --------------------------------------------------------------------------- evidence details


def test_non_inferiority_boundaries():
    policy = load_policy(POLICY_PATH)  # max_accuracy_drop 0.02
    at_margin = evaluate(_metrics(quality__accuracy__delta_ci=[-0.02, 0.03]), policy)
    assert _rule(at_margin, "quality.accuracy_drop").status == Status.PASS
    just_outside = evaluate(_metrics(quality__accuracy__delta_ci=[-0.08, -0.0201]), policy)
    assert _rule(just_outside, "quality.accuracy_drop").status == Status.FAIL
    touching = evaluate(_metrics(quality__accuracy__delta_ci=[-0.08, -0.02]), policy)
    assert _rule(touching, "quality.accuracy_drop").status == Status.INCONCLUSIVE


def test_absolute_limit_at_boundary_passes():
    d = evaluate(_metrics(quality__unsupported_claim_rate__candidate=0.1 + 0.2 - 0.27), load_policy(POLICY_PATH))
    assert _rule(d, "quality.unsupported_claim_rate").status == Status.PASS


def test_zero_margin_slice_passes_identical_models():
    d = evaluate(_metrics(slices__prompt_injection__accuracy__delta_ci=[0.0, 0.0]), load_policy(POLICY_PATH))
    assert _rule(d, "slice.prompt_injection.accuracy_drop").status == Status.PASS


def test_rule_records_evidence():
    d = evaluate(_metrics(), load_policy(POLICY_PATH))
    acc = _rule(d, "quality.accuracy_drop")
    assert (acc.evidence, acc.n, acc.ci, acc.limit, acc.direction) == ("ci", 300, (0.005, 0.055), -0.02, ">=")
    assert acc.observed == pytest.approx(0.03)
    assert acc.policy_field == "quality.max_accuracy_drop"
    err = _rule(d, "serving.error_rate")
    assert (err.evidence, err.observed, err.ci, err.limit, err.direction) == ("point_estimate", 0.003, None, 0.01, "<=")


def test_regression_rule_reports_percent_units():
    d = evaluate(
        _metrics(
            serving__p95_latency_ms__baseline=1500,
            serving__p95_latency_ms__candidate=3000,
            serving__p95_latency_ms__delta_ci=[1300, 1700],
        ),
        load_policy(POLICY_PATH),
    )
    r = _rule(d, "serving.p95_regression")
    assert (r.limit, r.direction, r.unit) == (25, "<=", "%")
    assert r.observed == pytest.approx(100.0)
    assert r.ci == pytest.approx((86.667, 113.333), abs=1e-3)
    assert _rule(d, "serving.p95_latency_ms").unit == "ms"


@pytest.mark.parametrize("seed", range(3))
def test_reported_values_agree_with_status(seed):
    """For every evaluated rule, re-deriving pass/fail from limit, direction and CI/observed matches status."""
    variants = [
        {},
        {"quality__accuracy__delta_ci": [-0.08, -0.03], "serving__p95_latency_ms__delta_ci": [1300, 1700]},
        {"quality__unsupported_claim_rate__candidate": 0.05, "cost__cost_per_valid_response__delta_ci": [0.0015, 0.0025]},
    ]
    d = evaluate(_metrics(**variants[seed]), load_policy(POLICY_PATH))
    ops = {"<=": lambda a, b: a <= b + 1e-9, ">=": lambda a, b: a >= b - 1e-9}
    for r in d.rules:
        if r.status not in (Status.PASS, Status.FAIL) or r.direction not in ops:
            continue
        ok = ops[r.direction]
        values = r.ci if r.evidence == "ci" else (r.observed,)
        if r.status == Status.PASS:
            assert all(ok(v, r.limit) for v in values), r
        else:
            assert not any(ok(v, r.limit) for v in values), r


def test_baseline_replay_skipped_is_reported():
    d = evaluate(_metrics(validity__baseline_replay_max_deviation=None), load_policy(POLICY_PATH))
    assert _rule(d, "validity.baseline_replay").status == Status.SKIPPED


def test_zero_baseline_relative_limit_is_inconclusive():
    d = evaluate(
        _metrics(cost__cost_per_valid_response__baseline=0.0, cost__cost_per_valid_response__delta_ci=[0.001, 0.002]),
        load_policy(POLICY_PATH),
    )
    assert _rule(d, "cost.cost_regression").status == Status.INCONCLUSIVE


# --------------------------------------------------------------------------- A/A


def test_identical_models_never_rejected():
    doc = json.loads(PASS_METRICS.read_text(encoding="utf-8"))
    for section in ("quality", "safety", "serving", "cost"):
        for c in doc[section].values():
            c["candidate"] = c["baseline"]
            c["delta_ci"] = [0.0, 0.0]
    for s in doc["slices"].values():
        for c in s.values():
            c["candidate"] = c["baseline"]
            c["delta_ci"] = [0.0, 0.0]
    d = evaluate(RunMetrics.model_validate(doc), load_policy(POLICY_PATH))
    assert d.outcome not in (Outcome.REJECT, Outcome.ROLLBACK)


# --------------------------------------------------------------------------- policy handling


def test_unconfigured_limit_is_not_evaluated():
    d = evaluate(_metrics(serving__timeout_rate=_DELETE), _policy(serving__max_timeout_rate=_DELETE))
    assert "serving.timeout_rate" not in {r.rule_id for r in d.rules}
    assert d.outcome == Outcome.PROMOTE


def test_policy_without_cost_section():
    d = evaluate(_metrics(), _policy(cost=_DELETE))
    assert not any(r.category == "cost" for r in d.rules)


def test_policy_typo_is_rejected():
    with pytest.raises(ValidationError):
        _policy(serving__max_p95_latancy_ms=4000)


def test_policy_hourly_cost_needs_rate():
    with pytest.raises(ValidationError):
        _policy(cost__hardware_hourly_rate_usd=_DELETE)


def test_metrics_reject_reversed_ci():
    with pytest.raises(ValidationError):
        _metrics(quality__accuracy__delta_ci=[0.05, 0.01])


def test_metrics_reject_nan():
    with pytest.raises(ValidationError):
        _metrics(serving__error_rate__candidate=float("nan"))


def test_policy_version_recorded():
    d = evaluate(_metrics(), _policy(policy_version=7))
    assert d.policy_version == 7


# --------------------------------------------------------------------------- determinism and exit codes


def test_decision_is_deterministic():
    m, p = _metrics(), load_policy(POLICY_PATH)
    assert evaluate(m, p).model_dump_json() == evaluate(copy.deepcopy(m), p).model_dump_json()


def test_decision_round_trips_json():
    from release_gate.gate import Decision

    d = evaluate(_metrics(serving__error_rate__candidate=0.03), load_policy(POLICY_PATH))
    assert Decision.model_validate_json(d.model_dump_json()) == d


@pytest.mark.parametrize(
    ("outcome", "code"),
    [(Outcome.PROMOTE, 0), (Outcome.HOLD, 1), (Outcome.REJECT, 2), (Outcome.ROLLBACK, 2), (Outcome.INVALID, 3)],
)
def test_exit_codes(outcome, code):
    assert EXIT_CODES[outcome] == code
