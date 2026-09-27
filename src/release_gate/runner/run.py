"""Evaluation runner: send every case of a suite split to baseline and candidate.

Both roles get identical cases, prompt and per-spec request settings. Requests are dispatched
in ABBA order (baseline/candidate for even cases, candidate/baseline for odd ones) with the
same concurrency limit per endpoint, after a warm-up, so neither role systematically sees a
warmer or less loaded GPU. Raw completions go to ``results.jsonl``; nothing is scored here.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import platform
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .. import registry
from ..adapters import ChatClient, chat_body
from ..generator import read_cases, read_manifest, validate_suite_dir
from ..generator.schema import SuiteCase
from ..prompts import load_prompt
from .manifest import (
    MANIFEST_FILE,
    RESULTS_FILE,
    ROLES,
    CaseResult,
    Environment,
    Execution,
    ModelRef,
    PromptRef,
    Role,
    RunManifest,
    Split,
    SuiteRef,
)

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
WARMUP_ALERT = "[ALERT] Warmup\nstatus: firing\nmetrics:\n  http_5xx_rate: 0.5%\nlogs:\n"


class RunError(RuntimeError):
    """The run cannot start or complete; no run directory is written."""


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    suite_dir: str
    split: Split = "release"
    prompt: str = "prompts/triage-v1.yaml"
    baseline: str  # model spec name or path
    candidate: str
    baseline_url: str  # OpenAI-compatible base URL, e.g. http://127.0.0.1:8001/v1
    candidate_url: str
    models_dir: str = str(registry.DEFAULT_MODELS_DIR)
    concurrency: int = Field(default=4, ge=1)
    timeout_s: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=1, ge=0)
    backoff_s: float = Field(default=0.5, ge=0)
    warmup_requests: int = Field(default=2, ge=0)
    environment_extra: dict[str, str] = {}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _git() -> tuple[str | None, bool | None]:
    root = Path(__file__).resolve().parents[3]
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10, check=True)
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    return head.stdout.strip() or None, bool(status.stdout.strip())


def _environment(extra: dict[str, str]) -> Environment:
    try:
        version = importlib.metadata.version("llm-release-gate")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"
    commit, dirty = _git()
    return Environment(
        python=sys.version.split()[0],
        platform=platform.platform(),
        release_gate_version=version,
        httpx_version=httpx.__version__,
        git_commit=commit,
        git_dirty=dirty,
        extra=dict(extra),
    )


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def _load_suite(config: RunConfig) -> tuple[SuiteRef, list[SuiteCase]]:
    suite_dir = Path(config.suite_dir)
    issues = validate_suite_dir(suite_dir)
    if issues:
        raise RunError(f"suite {suite_dir} has {len(issues)} validation issue(s), first: {issues[0]}")
    manifest = read_manifest(suite_dir)
    cases = [c for c in read_cases(suite_dir) if c.split == config.split]
    if not cases:
        raise RunError(f"suite {suite_dir} has no {config.split!r} cases")
    ids = "\n".join(c.case_id for c in cases)
    ref = SuiteRef(
        suite_version=manifest.suite_version,
        path=suite_dir.as_posix(),
        cases_sha256=manifest.cases_sha256,
        frozen=manifest.frozen,
        generator_version=manifest.generator_version,
        scoring_rules_version=manifest.scoring_rules_version,
        split=config.split,
        n_cases=len(cases),
        case_ids_sha256=hashlib.sha256(ids.encode("utf-8")).hexdigest(),
    )
    return ref, cases


def _jobs(cases: list[SuiteCase]) -> list[tuple[SuiteCase, Role]]:
    jobs: list[tuple[SuiteCase, Role]] = []
    for i, case in enumerate(cases):
        order = ROLES if i % 2 == 0 else ROLES[::-1]
        jobs += [(case, role) for role in order]
    return jobs


async def run_async(
    config: RunConfig,
    *,
    runs_root: str | Path = "runs",
    run_id: str | None = None,
    transports: dict[Role, httpx.AsyncBaseTransport] | None = None,
) -> Path:
    """Execute a run and write ``<runs_root>/<run_id>/``. Returns the run directory."""
    created = datetime.now(UTC)
    specs = {
        "baseline": registry.load_model_spec(config.baseline, config.models_dir),
        "candidate": registry.load_model_spec(config.candidate, config.models_dir),
    }
    run_id = run_id or f"{created:%Y%m%dT%H%M%SZ}-{specs['baseline'].name}-vs-{specs['candidate'].name}"
    if not _RUN_ID.match(run_id):
        raise RunError(f"invalid run id {run_id!r}")
    run_dir = Path(runs_root) / run_id
    if run_dir.exists():
        raise RunError(f"{run_dir} already exists; runs are never overwritten")

    suite_ref, cases = _load_suite(config)
    prompt_path = Path(config.prompt)
    prompt, prompt_sha = load_prompt(prompt_path)
    urls = {"baseline": config.baseline_url, "candidate": config.candidate_url}
    transports = transports or {}

    def body(role: Role, text: str) -> dict:
        return chat_body(specs[role], prompt.messages(text))

    clients = {
        role: ChatClient(
            urls[role],
            timeout_s=config.timeout_s,
            max_retries=config.max_retries,
            backoff_s=config.backoff_s,
            transport=transports.get(role),
        )
        for role in ROLES
    }
    async with clients["baseline"], clients["candidate"]:
        served: dict[Role, list[str]] = {}
        for role in ROLES:
            try:
                served[role] = await clients[role].list_models()
            except (httpx.HTTPError, ValueError) as exc:
                raise RunError(f"{role} endpoint {urls[role]} failed preflight: {exc}") from exc
            if specs[role].name not in served[role]:
                raise RunError(f"{role} endpoint {urls[role]} serves {served[role]}, not {specs[role].name!r}")

        warmup_errors = {role: 0 for role in ROLES}
        for _ in range(config.warmup_requests):
            for role in ROLES:
                if (await clients[role].chat(body(role, WARMUP_ALERT))).error is not None:
                    warmup_errors[role] += 1

        limits = {role: asyncio.Semaphore(config.concurrency) for role in ROLES}
        t0 = time.perf_counter()

        async def one(seq: int, case: SuiteCase, role: Role, dispatched_ms: float) -> CaseResult:
            try:
                completion = await clients[role].chat(body(role, case.input_text))
            finally:
                limits[role].release()
            return CaseResult(case_id=case.case_id, role=role, seq=seq, dispatched_ms=dispatched_ms, completion=completion)

        tasks = []
        for seq, (case, role) in enumerate(_jobs(cases)):
            await limits[role].acquire()  # in-order dispatch keeps both roles in lockstep
            tasks.append(asyncio.create_task(one(seq, case, role, (time.perf_counter() - t0) * 1000)))
        results = await asyncio.gather(*tasks)

    finished = datetime.now(UTC)
    order = {c.case_id: i for i, c in enumerate(cases)}
    results = sorted(results, key=lambda r: (order[r.case_id], ROLES.index(r.role)))
    models = {}
    for role in ROLES:
        spec = specs[role]
        request = {k: v for k, v in body(role, "").items() if k != "messages"}
        models[role] = ModelRef(
            spec_name=spec.name,
            spec_sha256=_sha256(registry.spec_path(getattr(config, role), config.models_dir)),
            model_id=spec.model.id,
            revision=spec.model.revision,
            revision_pinned=spec.revision_pinned,
            dtype=spec.serving.dtype,
            quantization=spec.serving.quantization,
            base_url=urls[role],
            served_models=served[role],
            request=request,
        )
    manifest = RunManifest(
        run_id=run_id,
        created_at=_iso(created),
        finished_at=_iso(finished),
        duration_s=round((finished - created).total_seconds(), 3),
        suite=suite_ref,
        prompt=PromptRef(prompt_version=prompt.prompt_version, path=prompt_path.as_posix(), sha256=prompt_sha),
        models=models,
        execution=Execution(
            concurrency=config.concurrency,
            timeout_s=config.timeout_s,
            max_retries=config.max_retries,
            backoff_s=config.backoff_s,
            warmup_requests=config.warmup_requests,
            warmup_errors=warmup_errors,
        ),
        environment=_environment(config.environment_extra),
        n_results=len(results),
    )

    run_dir.mkdir(parents=True)
    lines = "".join(json.dumps(r.model_dump(mode="json"), sort_keys=True, ensure_ascii=False) + "\n" for r in results)
    (run_dir / RESULTS_FILE).write_text(lines, encoding="utf-8", newline="\n")
    _write_json(run_dir / MANIFEST_FILE, manifest.model_dump(mode="json"))
    return run_dir


def run(config: RunConfig, **kwargs) -> Path:
    return asyncio.run(run_async(config, **kwargs))
