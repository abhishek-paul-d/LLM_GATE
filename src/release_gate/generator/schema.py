"""File formats for synthetic suites: case records, suite config, manifest, and review log.

A suite version lives in ``suites/<suite_version>/``:
- ``cases.jsonl``  one ``SuiteCase`` per line, byte-identical for a given config and seed
- ``manifest.json`` counts, config echo, sha256 of cases.jsonl, frozen flag
- ``review.json``  human review status per case, keyed to each case's content hash
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..schemas.triage import Category
from .severity import Severity, SymptomKind

GENERATOR_VERSION = "0.2.0"
SCORING_RULES_VERSION = 1

Variant = Literal[
    "clear", "recovered", "missing_evidence", "conflicting", "prompt_injection", "long_input", "malformed_input",
]  # fmt: skip
VARIANTS: tuple[Variant, ...] = (
    "clear", "recovered", "missing_evidence", "conflicting", "prompt_injection", "long_input", "malformed_input",
)  # fmt: skip
Split = Literal["dev", "release"]
Behavior = Literal["answer", "ask_for_signal"]
Difficulty = Literal["easy", "medium", "hard"]

_SLUG = r"^[a-z0-9][a-z0-9._-]*$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class MetricRecord(_Strict):
    name: str
    value: float
    unit: Literal["%", "GiB", "MiB", "ms", "count", "millicores"]
    limit: float | None = None
    display: str  # exactly as rendered in input_text


class SymptomRecord(_Strict):
    kind: SymptomKind
    metric: MetricRecord
    recovered: bool = False


class Injection(_Strict):
    text: str  # the injected instruction, verbatim in input_text
    target_field: Literal["severity", "category"]
    target_value: str  # the value an obedient model would output; always differs from the label


class Expected(_Strict):
    category: Category  # the reference label
    # Categories a scorer accepts. Equal to [category] except for conflicting evidence, where
    # naming either supported cause is defensible; those cases are excluded from exact-label
    # accuracy and scored on whether the output mentions both signals (required_facts).
    acceptable_categories: list[Category] = Field(min_length=1)
    severity: Severity
    behavior: Behavior
    # Each inner list is one fact; mentioning any one alternative satisfies it. At least one
    # alternative of every fact appears in input_text (labels follow from input evidence).
    required_facts: list[list[str]] = Field(min_length=1)
    # Resources a safe next_check may reference. The resource named in the alert is always first.
    next_check_targets: list[str] = Field(min_length=1)


class SuiteCase(_Strict):
    case_id: str
    suite_version: str
    scenario_id: str  # family/template, e.g. "memory_pressure/a"; the unit of split holdout
    family: str
    variant: Variant
    split: Split
    seed: int
    difficulty: Difficulty
    tags: list[str]
    started_at: str
    resolved_at: str | None = None
    input_text: str
    symptom: SymptomRecord
    metrics: list[MetricRecord]  # every metric shown in input_text, symptom first
    entities: list[str]  # every concrete value the input states; each appears verbatim in input_text
    injection: Injection | None = None
    expected: Expected
    scoring_rules_version: int = SCORING_RULES_VERSION


class SuiteConfig(_Strict):
    suite_version: str = Field(pattern=_SLUG)
    seed: int
    families: list[str] = Field(min_length=1)
    variants: list[Variant] = Field(min_length=1)
    cases_per_cell: int = Field(default=1, ge=1)
    # Per-variant override of cases_per_cell, e.g. more cases for protected slices so they
    # clear the policy's minimum_cases_per_slice in the release split.
    cases_per_variant: dict[Variant, Annotated[int, Field(ge=1)]] = {}
    # Whole templates held out of development; everything else is dev.
    release_templates: list[str] = []


class SuiteManifest(_Strict):
    suite_version: str
    generator_version: str
    scoring_rules_version: int
    seed: int
    config: SuiteConfig
    n_cases: int
    counts: dict[str, dict[str, int]]
    cases_sha256: str
    frozen: bool = False


class ReviewRecord(_Strict):
    status: Literal["candidate", "approved", "rejected"] = "candidate"
    case_sha256: str  # review applies to this exact case content; a regenerated case is stale
    reviewer: str | None = None
    notes: str = ""
