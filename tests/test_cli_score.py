import json
from pathlib import Path

import pytest
import yaml

from release_gate.cli import main
from release_gate.evaluation import (
    DECISION_FILE,
    EVALUATION_FILE,
    METRICS_FILE,
    POLICY_FILE,
    REPORT_FILE,
    SCORES_FILE,
)
from release_gate.mock import PERSONAS, MockBackend
from release_gate.runner import RunConfig, run

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "suites" / "starter-v1"
BASELINE, CANDIDATE = "llama-3.1-8b-instruct-fp8", "qwen3-8b-fp8"


@pytest.fixture
def policy_path(tmp_path: Path) -> Path:
    """policy_v1 without cost limits and with small protected-slice minimums."""
    policy = yaml.safe_load((ROOT / "policies/policy_v1.yaml").read_text(encoding="utf-8"))
    policy.pop("cost")
    policy["decision"]["minimum_cases_per_slice"] = 3
    policy["decision"]["bootstrap_resamples"] = 500
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


def _run(tmp_path: Path, candidate: str, baseline: str = "reference") -> Path:
    fast = {name: PERSONAS[name].model_copy(update={"base_latency_ms": 1.0, "jitter_ms": 2.0}) for name in (baseline, candidate)}
    transports = {
        "baseline": MockBackend(fast[baseline], BASELINE).transport(),
        "candidate": MockBackend(fast[candidate], CANDIDATE).transport(),
    }
    config = RunConfig(
        suite_dir=str(SUITE),
        prompt=str(ROOT / "prompts/triage-v1.yaml"),
        baseline=BASELINE,
        candidate=CANDIDATE,
        baseline_url="http://baseline/v1",
        candidate_url="http://candidate/v1",
        models_dir=str(ROOT / "models"),
        backoff_s=0,
        warmup_requests=0,
    )
    return run(config, runs_root=tmp_path / "runs", run_id=f"{baseline}-vs-{candidate}", transports=transports)


def test_score_rejects_then_replay_reproduces_and_detects_changes(
    tmp_path: Path, policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _run(tmp_path, "unsafe")
    args = ["score", str(run_dir), "--policy", str(policy_path)]

    assert main(args) == 2
    output = capsys.readouterr()
    assert f"wrote {run_dir / DECISION_FILE} and {run_dir / REPORT_FILE}" in output.out
    assert "REJECT (exit 2)" in output.err
    for name in (DECISION_FILE, METRICS_FILE, SCORES_FILE, REPORT_FILE, POLICY_FILE, EVALUATION_FILE):
        assert (run_dir / name).is_file()

    assert main(args) == 4
    assert "already scored" in capsys.readouterr().err

    assert main(["replay", str(run_dir)]) == 2
    output = capsys.readouterr()
    assert "replay reproduces" in output.out
    assert "REJECT (exit 2)" in output.err

    saved = json.loads((run_dir / DECISION_FILE).read_text(encoding="utf-8"))
    saved["outcome"] = "PROMOTE"
    (run_dir / DECISION_FILE).write_text(json.dumps(saved), encoding="utf-8")
    assert main(["replay", str(run_dir)]) == 3
    err = capsys.readouterr().err
    assert DECISION_FILE in err
    assert "cause:" not in err  # same code, so the saved files were edited


def test_replay_names_a_scorer_version_change_as_the_cause(
    tmp_path: Path, policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _run(tmp_path, "unsafe")
    assert main(["score", str(run_dir), "--policy", str(policy_path)]) == 2
    record = json.loads((run_dir / EVALUATION_FILE).read_text(encoding="utf-8"))
    record["scorer_version"] = "0.9.0"
    (run_dir / EVALUATION_FILE).write_text(json.dumps(record), encoding="utf-8")
    capsys.readouterr()

    assert main(["replay", str(run_dir)]) == 3
    assert "cause: scorer_version 0.9.0 -> " in capsys.readouterr().err


def test_replay_policy_what_if_does_not_change_saved_decision(
    tmp_path: Path, policy_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _run(tmp_path, "unsafe")
    assert main(["score", str(run_dir), "--policy", str(policy_path)]) == 2
    capsys.readouterr()
    original = (run_dir / DECISION_FILE).read_bytes()

    lenient = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    lenient["safety"]["max_unsafe_command_rate"] = 1.0
    lenient_path = tmp_path / "lenient.yaml"
    lenient_path.write_text(yaml.safe_dump(lenient), encoding="utf-8")
    assert main(["replay", str(run_dir), "--policy", str(lenient_path)]) == 0
    output = capsys.readouterr()
    assert "what-if" in output.out
    assert (run_dir / DECISION_FILE).read_bytes() == original


def test_replay_unscored_run_returns_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = _run(tmp_path, "reference")

    assert main(["replay", str(run_dir)]) == 4
    assert "gate score" in capsys.readouterr().err


def test_replay_resolves_run_id_under_default_runs_root(
    tmp_path: Path, policy_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = _run(tmp_path, "unsafe")
    assert main(["score", str(run_dir), "--policy", str(policy_path)]) == 2
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)

    assert main(["replay", run_dir.name]) == 2
    assert "replay reproduces" in capsys.readouterr().out


def test_score_missing_policy_returns_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = _run(tmp_path, "unsafe")

    assert main(["score", str(run_dir), "--policy", str(tmp_path / "missing.yaml")]) == 4
    assert "cannot load policy" in capsys.readouterr().err


def test_score_with_cost_policy_returns_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = _run(tmp_path, "reference", baseline="reference")

    assert main(["score", str(run_dir), "--policy", str(ROOT / "policies/policy_v1.yaml")]) == 3
    output = capsys.readouterr()
    assert "INVALID (exit 3)" in output.err


def test_score_and_replay_without_required_arguments_use_exit_code_4() -> None:
    for args in (["score"], ["replay"]):
        with pytest.raises(SystemExit) as error:
            main(list(args))
        assert error.value.code == 4
