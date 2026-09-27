"""Runner against in-process mock endpoints: run directory, manifest, ABBA order, retries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from release_gate.generator import write_suite
from release_gate.generator.build import load_config
from release_gate.mock import PERSONAS, MockBackend
from release_gate.runner import MANIFEST_FILE, RESULTS_FILE, CaseResult, RunConfig, RunError, RunManifest, run

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "suites" / "starter-v1"
BASELINE, CANDIDATE = "llama-3.1-8b-instruct-fp8", "qwen3-8b-fp8"


def _fast(name: str, **changes):
    return PERSONAS[name].model_copy(update={"base_latency_ms": 1.0, "jitter_ms": 2.0, **changes})


def _run(tmp_path, baseline="reference", candidate="reference", served=(BASELINE, CANDIDATE), **config):
    backends = {"baseline": MockBackend(_fast(baseline), served[0]), "candidate": MockBackend(_fast(candidate), served[1])}
    cfg = RunConfig(
        suite_dir=str(config.pop("suite_dir", SUITE)),
        prompt=str(ROOT / "prompts/triage-v1.yaml"),
        baseline=BASELINE,
        candidate=CANDIDATE,
        baseline_url="http://baseline/v1",
        candidate_url="http://candidate/v1",
        models_dir=str(ROOT / "models"),
        backoff_s=0,
        **config,
    )
    transports = {role: b.transport() for role, b in backends.items()}
    return run(cfg, runs_root=tmp_path / "runs", run_id=config.get("run_id", "t1"), transports=transports)


def _read(run_dir: Path) -> tuple[RunManifest, list[CaseResult]]:
    manifest = RunManifest.model_validate_json((run_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    lines = (run_dir / RESULTS_FILE).read_text(encoding="utf-8").splitlines()
    return manifest, [CaseResult.model_validate_json(line) for line in lines]


def test_run_writes_manifest_and_paired_results(tmp_path):
    manifest, results = _read(_run(tmp_path))
    assert manifest.suite.split == "release" and manifest.suite.n_cases == 15 and manifest.n_results == 30
    assert manifest.suite.cases_sha256 == json.loads((SUITE / "manifest.json").read_text())["cases_sha256"]
    assert len(manifest.prompt.sha256) == 64 and manifest.prompt.prompt_version == "triage-v1"
    assert manifest.models["candidate"].request["chat_template_kwargs"] == {"enable_thinking": False}
    assert manifest.models["baseline"].served_models == [BASELINE]
    assert manifest.execution.warmup_requests == 2 and manifest.environment.python
    assert all(r.completion.error is None and r.completion.text for r in results)
    by_case: dict[str, dict[str, int]] = {}
    for r in results:
        by_case.setdefault(r.case_id, {})[r.role] = r.seq
    assert len(by_case) == 15
    firsts = ["baseline" if s["baseline"] < s["candidate"] else "candidate" for s in by_case.values()]
    assert firsts[:4] == ["baseline", "candidate", "baseline", "candidate"]  # ABBA dispatch


def test_retries_are_recorded(tmp_path):
    _, results = _read(_run(tmp_path, candidate="flaky", max_retries=1))
    flaky = [r.completion for r in results if r.role == "candidate"]
    assert any(c.first_error == "http_5xx" and c.error is None for c in flaky)
    assert all(c.error is None for c in flaky)


def test_runs_are_never_overwritten(tmp_path):
    _run(tmp_path)
    with pytest.raises(RunError, match="already exists"):
        _run(tmp_path)


def test_preflight_rejects_wrong_served_model(tmp_path):
    with pytest.raises(RunError, match="serves"):
        _run(tmp_path, served=(BASELINE, "some-other-model"))
    assert not (tmp_path / "runs" / "t1").exists()


def test_invalid_suite_is_refused(tmp_path):
    suite_dir = write_suite(load_config(ROOT / "suites/configs/starter-v1.yaml"), tmp_path / "suites")
    (suite_dir / "cases.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(RunError, match="validation issue"):
        _run(tmp_path, suite_dir=suite_dir)
