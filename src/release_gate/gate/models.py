"""Data contract for the release gate: policy (input), run metrics (input), decision (output).

All models forbid unknown fields so a typo in a policy or metrics file fails loudly
instead of silently disabling a limit.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


Rate = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegative = Annotated[float, Field(ge=0.0)]


# --------------------------------------------------------------------------- policy


class QualityLimits(_Strict):
    max_accuracy_drop: Rate | None = None
    max_unsupported_claim_rate: Rate | None = None
    min_schema_valid_rate: Rate | None = None


class SafetyLimits(_Strict):
    max_unsafe_command_rate: Rate | None = None
    max_injection_compliance_rate: Rate | None = None


class ServingLimits(_Strict):
    max_p95_latency_ms: NonNegative | None = None
    max_p95_regression_pct: NonNegative | None = None
    max_error_rate: Rate | None = None
    max_timeout_rate: Rate | None = None


class CostPolicy(_Strict):
    model: Literal["hourly_hardware", "per_token"]
    hardware_hourly_rate_usd: NonNegative | None = None
    max_cost_per_valid_response: NonNegative | None = None
    max_cost_regression_pct: NonNegative | None = None

    @model_validator(mode="after")
    def _rate_required_for_hourly(self) -> CostPolicy:
        if self.model == "hourly_hardware" and self.hardware_hourly_rate_usd is None:
            raise ValueError("cost.model 'hourly_hardware' requires hardware_hourly_rate_usd")
        return self


class SliceLimits(_Strict):
    max_accuracy_drop: float = Field(ge=0.0, le=1.0)


class DecisionSettings(_Strict):
    minimum_cases_per_slice: int = Field(ge=1)
    confidence_level: float = Field(gt=0.0, lt=1.0)
    bootstrap_resamples: int = Field(ge=100)
    bootstrap_seed: int


class ValiditySettings(_Strict):
    baseline_replay_tolerance: float = Field(ge=0.0)
    max_infra_error_rate: float = Field(ge=0.0, le=1.0)


class Policy(_Strict):
    policy_version: int = Field(ge=1)
    quality: QualityLimits = QualityLimits()
    safety: SafetyLimits = SafetyLimits()
    serving: ServingLimits = ServingLimits()
    cost: CostPolicy | None = None
    protected_slices: dict[str, SliceLimits] = {}
    decision: DecisionSettings
    validity: ValiditySettings


def load_policy(path: str | Path) -> Policy:
    with open(path, encoding="utf-8") as f:
        return Policy.model_validate(yaml.safe_load(f))


# --------------------------------------------------------------------------- metrics


class Comparison(_Strict):
    """One metric measured on baseline and candidate over the same n paired cases.

    ``delta_ci`` is the confidence interval of (candidate - baseline), in the metric's
    own units, computed by the stats module at ``RunMetrics.stats.confidence_level``.
    """

    baseline: float
    candidate: float
    n: int = Field(ge=0)
    delta_ci: tuple[float, float] | None = None

    @model_validator(mode="after")
    def _ci_ordered(self) -> Comparison:
        if self.delta_ci is not None and self.delta_ci[0] > self.delta_ci[1]:
            raise ValueError(f"delta_ci lower bound exceeds upper bound: {self.delta_ci}")
        return self

    @property
    def delta(self) -> float:
        return self.candidate - self.baseline


class StatsInfo(_Strict):
    method: str
    confidence_level: float = Field(gt=0.0, lt=1.0)
    resamples: int = Field(ge=0)
    seed: int


class RunValidity(_Strict):
    baseline_healthy: bool
    infra_error_rate: float = Field(ge=0.0, le=1.0)
    # Largest absolute deviation of replayed baseline metrics from its approved record.
    # None when no approved record exists yet (first run of a baseline).
    baseline_replay_max_deviation: float | None = Field(default=None, ge=0.0)
    manifest_mismatches: list[str] = []


class RunMetrics(_Strict):
    schema_version: Literal[1]
    run_id: str = Field(min_length=1)
    mode: Literal["pre_deploy", "canary"]
    stats: StatsInfo
    validity: RunValidity
    quality: dict[str, Comparison] = {}
    safety: dict[str, Comparison] = {}
    serving: dict[str, Comparison] = {}
    cost: dict[str, Comparison] = {}
    slices: dict[str, dict[str, Comparison]] = {}


def load_metrics(path: str | Path) -> RunMetrics:
    return RunMetrics.model_validate_json(Path(path).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- decision


class Outcome(StrEnum):
    PROMOTE = "PROMOTE"
    HOLD = "HOLD"
    REJECT = "REJECT"
    ROLLBACK = "ROLLBACK"
    INVALID = "INVALID"


EXIT_CODES: dict[Outcome, int] = {
    Outcome.PROMOTE: 0,
    Outcome.HOLD: 1,
    Outcome.REJECT: 2,
    Outcome.ROLLBACK: 2,
    Outcome.INVALID: 3,
}


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"  # CI straddles the limit, or no CI for a comparative limit
    INSUFFICIENT = "insufficient"  # protected slice below minimum_cases_per_slice
    MISSING = "missing"  # policy sets a limit but the run produced no usable metric
    SKIPPED = "skipped"  # check not applicable to this run; does not affect the outcome


Category = Literal["validity", "safety", "quality", "serving", "cost", "slice"]


class RuleResult(_Strict):
    """One evaluated rule. ``limit``, ``observed`` and ``ci`` share one unit (``unit``).

    The rule passes when ``observed <direction> limit`` holds (for ``evidence == "ci"``, when
    it holds for the whole CI). Comparative rules report the paired delta (drop rules, with
    ``limit = -max_drop``) or the percent change (regression rules), so ``limit`` may differ
    from the raw policy value named by ``policy_field``. Point-estimate rules have no ``ci``.
    """

    rule_id: str
    category: Category
    status: Status
    message: str
    metric: str | None = None
    policy_field: str | None = None
    limit: float | None = None
    direction: Literal["<=", ">=", "=="] | None = None
    unit: str | None = None
    observed: float | None = None
    ci: tuple[float, float] | None = None
    n: int | None = None
    evidence: Literal["ci", "point_estimate", "check"] | None = None


class Decision(_Strict):
    gate_version: str
    run_id: str
    policy_version: int
    mode: Literal["pre_deploy", "canary"]
    outcome: Outcome
    reason: str
    triggered_rules: list[str]
    rules: list[RuleResult]

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]
