"""Run directory formats: ``runs/<run_id>/manifest.json`` and ``results.jsonl``.

The manifest pins everything a decision depends on (suite, prompt, model specs, request
settings, execution settings, environment) by content hash, so a saved run can be scored,
gated and replayed later and any drift in its inputs is detectable.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..adapters import Completion

MANIFEST_FILE = "manifest.json"
RESULTS_FILE = "results.jsonl"
RUN_SCHEMA_VERSION = 1

Role = Literal["baseline", "candidate"]
ROLES: tuple[Role, Role] = ("baseline", "candidate")
Split = Literal["release", "dev"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SuiteRef(_Strict):
    suite_version: str
    path: str
    cases_sha256: str  # sha256 of the suite's cases.jsonl
    frozen: bool
    generator_version: str
    scoring_rules_version: int
    split: Split
    n_cases: int  # cases in the selected split
    case_ids_sha256: str  # sha256 of the selected case ids, newline-joined, in run order


class PromptRef(_Strict):
    prompt_version: str
    path: str
    sha256: str


class ModelRef(_Strict):
    spec_name: str
    spec_sha256: str
    model_id: str
    revision: str
    revision_pinned: bool
    dtype: str
    quantization: str | None
    base_url: str
    served_models: list[str]  # reported by the endpoint's /models at preflight
    request: dict[str, Any]  # request body sent for every case, minus the messages


class Execution(_Strict):
    mode: Literal["concurrent"] = "concurrent"
    # Dispatch order per case pair: even cases baseline first, odd cases candidate first.
    order: Literal["abba"] = "abba"
    concurrency: int = Field(ge=1)  # max in-flight requests per endpoint
    timeout_s: float
    max_retries: int
    backoff_s: float
    warmup_requests: int
    warmup_errors: dict[str, int]


class Environment(_Strict):
    python: str
    platform: str
    release_gate_version: str
    httpx_version: str
    git_commit: str | None
    git_dirty: bool | None
    extra: dict[str, str] = {}  # e.g. GPU, driver, CUDA and vLLM versions from the Colab notebook


class RunManifest(_Strict):
    schema_version: Literal[1] = RUN_SCHEMA_VERSION
    run_id: str
    created_at: str
    finished_at: str
    duration_s: float
    suite: SuiteRef
    prompt: PromptRef
    models: dict[Role, ModelRef]
    execution: Execution
    environment: Environment
    n_results: int


class CaseResult(_Strict):
    """One request: a case sent to one role. The completion text is untrusted model output."""

    case_id: str
    role: Role
    seq: int  # dispatch order across the whole run
    dispatched_ms: float  # offset from the start of the measured phase
    completion: Completion
