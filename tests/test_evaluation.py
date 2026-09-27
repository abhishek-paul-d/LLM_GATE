"""Scoring, metrics and replay of saved runs against in-process mock endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from release_gate.adapters.openai_compat import Attempt, Completion
from release_gate.evaluation import (
    DECISION_FILE,
    EVALUATION_FILE,
    METRICS_FILE,
    POLICY_FILE,
    REPORT_FILE,
    EvaluationError,
    evaluate_run,
    replay_run,
    write_evaluation,
)
from release_gate.gate import load_policy
from release_gate.generator import read_cases
from release_gate.metrics import build_metrics, pair_scores
from release_gate.mock import PERSONAS, MockBackend
from release_gate.runner import RunConfig, run
from release_gate.scorers import score_case

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "suites" / "starter-v1"
BASELINE, CANDIDATE = "llama-3.1-8b-instruct-fp8", "qwen3-8b-fp8"


@pytest.fixture
def policy_path(tmp_path) -> Path:
    """policy_v1 without cost limits (cost metrics are not produced yet) and small protected-slice minimums."""
    policy = yaml.safe_load((ROOT / "policies/policy_v1.yaml").read_text(encoding="utf-8"))
    policy.pop("cost")
    policy["decision"]["minimum_cases_per_slice"] = 3
    policy["decision"]["bootstrap_resamples"] = 500
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(policy), encoding="utf-8")
    return path


def _run(tmp_path: Path, candidate: str, baseline: str = "reference", suite: Path = SUITE) -> Path:
    fast = {n: PERSONAS[n].model_copy(update={"base_latency_ms": 1.0, "jitter_ms": 2.0}) for n in (baseline, candidate)}
    transports = {
        "baseline": MockBackend(fast[baseline], BASELINE).transport(),
        "candidate": MockBackend(fast[candidate], CANDIDATE).transport(),
    }
    config = RunConfig(
        suite_dir=str(suite),
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


@pytest.mark.parametrize(
    ("candidate", "outcome", "rule"),
    [
        ("reference", "PROMOTE", None),  # A/A
        ("unsafe", "REJECT", "safety.unsafe_command_rate"),
        ("obedient", "REJECT", "safety.injection_compliance_rate"),
        ("broken-json", "REJECT", "quality.schema_valid_rate"),
        ("flaky", "REJECT", "serving.error_rate"),
    ],
)
def test_personas_produce_the_expected_decision(tmp_path, policy_path, candidate, outcome, rule):
    evaluation = evaluate_run(_run(tmp_path, candidate), policy_path)
    assert evaluation.decision.outcome.value == outcome
    if rule:
        assert rule in evaluation.decision.triggered_rules
    assert len(evaluation.scores) == 30 and evaluation.metrics.stats.resamples == 500


def test_a_a_comparison_never_rejects_under_policy_v2(tmp_path):
    """Under the shipped milestone policy the starter suite is too small to promote; it must hold, not reject."""
    decision = evaluate_run(_run(tmp_path, "reference"), ROOT / "policies/policy_v2.yaml").decision
    assert decision.outcome.value == "HOLD"
    assert all(r.startswith("slice.") for r in decision.triggered_rules)


def test_missing_cost_metrics_make_policy_v1_invalid(tmp_path):
    decision = evaluate_run(_run(tmp_path, "reference"), ROOT / "policies/policy_v1.yaml").decision
    assert decision.outcome.value == "INVALID"
    assert "cost.cost_per_valid_response" in decision.triggered_rules


def test_write_then_replay_reproduces_every_file(tmp_path, policy_path):
    run_dir = _run(tmp_path, "unsafe")
    write_evaluation(run_dir, evaluate_run(run_dir, policy_path))
    assert (run_dir / POLICY_FILE).read_bytes() == policy_path.read_bytes()
    policy_path.unlink()  # replay must use the run's own policy copy
    evaluation, differences = replay_run(run_dir)
    assert differences == [] and evaluation.decision.outcome.value == "REJECT"
    with pytest.raises(EvaluationError, match="already scored"):
        write_evaluation(run_dir, evaluation)


def test_interrupted_write_leaves_the_run_unscored(tmp_path, policy_path, monkeypatch):
    run_dir = _run(tmp_path, "unsafe")
    evaluation = evaluate_run(run_dir, policy_path)
    real_write = Path.write_text

    def failing(self, text, *args, **kwargs):
        if self.name == REPORT_FILE:
            raise OSError("disk full")
        return real_write(self, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing)
    with pytest.raises(OSError):
        write_evaluation(run_dir, evaluation)
    monkeypatch.undo()
    assert not (run_dir / EVALUATION_FILE).exists()
    write_evaluation(run_dir, evaluation)  # can be scored again
    assert replay_run(run_dir)[1] == []


def test_replay_with_inputs_moved_elsewhere_still_matches(tmp_path, policy_path):
    run_dir = _run(tmp_path, "unsafe")
    write_evaluation(run_dir, evaluate_run(run_dir, policy_path))
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    for name in ("cases.jsonl", "manifest.json", "review.json"):
        (moved / name).write_bytes((SUITE / name).read_bytes())
    _, differences = replay_run(run_dir, suite_dir=moved)
    assert differences == []


def test_replay_detects_an_edited_decision(tmp_path, policy_path):
    run_dir = _run(tmp_path, "unsafe")
    write_evaluation(run_dir, evaluate_run(run_dir, policy_path))
    saved = json.loads((run_dir / DECISION_FILE).read_text(encoding="utf-8"))
    saved["outcome"] = "PROMOTE"
    (run_dir / DECISION_FILE).write_text(json.dumps(saved), encoding="utf-8")
    _, differences = replay_run(run_dir)
    assert differences == [DECISION_FILE]


def test_replay_under_another_policy_is_a_what_if(tmp_path, policy_path):
    run_dir = _run(tmp_path, "unsafe")
    write_evaluation(run_dir, evaluate_run(run_dir, policy_path))
    lenient = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    lenient["safety"]["max_unsafe_command_rate"] = 1.0
    other = tmp_path / "lenient.yaml"
    other.write_text(yaml.safe_dump(lenient), encoding="utf-8")
    evaluation, differences = replay_run(run_dir, other)
    assert differences == [] and "safety.unsafe_command_rate" not in evaluation.decision.triggered_rules
    assert json.loads((run_dir / DECISION_FILE).read_text(encoding="utf-8"))["outcome"] == "REJECT"


def test_changed_suite_makes_the_run_invalid(tmp_path, policy_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    for name in ("cases.jsonl", "manifest.json", "review.json"):
        (suite / name).write_bytes((SUITE / name).read_bytes())
    run_dir = _run(tmp_path, "reference", suite=suite)
    with (suite / "cases.jsonl").open("a", encoding="utf-8") as f:
        f.write("\n")  # same cases, different bytes
    decision = evaluate_run(run_dir, policy_path).decision
    assert decision.outcome.value == "INVALID" and "validity.manifest" in decision.triggered_rules


def test_changed_prompt_makes_the_run_invalid(tmp_path, policy_path):
    run_dir = _run(tmp_path, "reference")
    prompt = tmp_path / "prompt.yaml"
    prompt.write_text((ROOT / "prompts/triage-v1.yaml").read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")
    decision = evaluate_run(run_dir, policy_path, prompt_path=prompt).decision
    assert decision.outcome.value == "INVALID"


def test_unreadable_runs_and_policies_are_errors(tmp_path, policy_path):
    with pytest.raises(EvaluationError, match="not a readable run"):
        evaluate_run(tmp_path / "nope", policy_path)
    run_dir = _run(tmp_path, "reference")
    with pytest.raises(EvaluationError, match="cannot load policy"):
        evaluate_run(run_dir, tmp_path / "missing.yaml")
    with pytest.raises(EvaluationError, match="no saved evaluation"):
        replay_run(run_dir)
    (run_dir / EVALUATION_FILE).write_text("{}", encoding="utf-8")
    with pytest.raises(EvaluationError, match="no saved evaluation"):
        replay_run(run_dir)
    assert not (run_dir / METRICS_FILE).exists()


# --------------------------------------------------------------------------- metrics assembly


DEV = [c for c in read_cases(SUITE) if c.split == "dev"]


def _scores(texts: dict[str, str | None] | None = None, *, errors=None):
    """Scores for every dev case, both roles answering correctly unless overridden per (case_id, role)."""
    out = []
    for case in DEV:
        for role in ("baseline", "candidate"):
            key = f"{case.case_id}:{role}"
            text = (texts or {}).get(key, _correct(case))
            attempt_errors = (errors or {}).get(key, (None,))
            completion = Completion(
                text=text, attempts=[Attempt(error=e, latency_ms=10.0) for e in attempt_errors], total_ms=10.0
            )
            out.append(score_case(case, role, completion, case.input_text))
    return out


def _correct(case) -> str:
    target = case.expected.next_check_targets[0]
    return json.dumps(
        {
            "category": case.expected.category,
            "severity": case.expected.severity,
            "summary": f"{target} alert",
            "next_check": f"kubectl describe deployment/{target}",
        }
    )


POLICY = load_policy(ROOT / "policies/policy_v1.yaml").model_copy(
    update={"decision": load_policy(ROOT / "policies/policy_v1.yaml").decision.model_copy(update={"bootstrap_resamples": 200})}
)


def test_metrics_cover_the_gate_names_and_case_sets():
    metrics = build_metrics(_scores(), run_id="r", policy=POLICY)
    n_conflicting = sum(c.variant == "conflicting" for c in DEV)
    assert metrics.quality["accuracy"].n == len(DEV) - n_conflicting
    assert metrics.quality["schema_valid_rate"].n == len(DEV)
    assert metrics.safety["injection_compliance_rate"].n == sum(c.injection is not None for c in DEV)
    assert {"p95_latency_ms", "error_rate", "timeout_rate"} <= metrics.serving.keys()
    assert {"prompt_injection", "missing_evidence", "family:memory_pressure"} <= metrics.slices.keys()
    assert metrics.quality["accuracy"].delta_ci == (0.0, 0.0)
    assert metrics.validity.baseline_healthy and metrics.validity.infra_error_rate == 0.0


def test_transport_errors_are_infrastructure_not_model_errors():
    first = DEV[0].case_id
    scores = _scores(errors={f"{first}:candidate": ("transport", None), f"{DEV[1].case_id}:candidate": ("timeout", None)})
    metrics = build_metrics(scores, run_id="r", policy=POLICY)
    assert metrics.validity.infra_error_rate == pytest.approx(1 / (2 * len(DEV)))
    assert metrics.serving["error_rate"].candidate == pytest.approx(1 / len(DEV))  # the timeout only
    assert metrics.serving["timeout_rate"].candidate == pytest.approx(1 / len(DEV))


def test_unhealthy_baseline():
    errors = {f"{c.case_id}:baseline": ("http_5xx",) for c in DEV[:3]}
    texts = {f"{c.case_id}:baseline": None for c in DEV[:3]}
    metrics = build_metrics(_scores(texts, errors=errors), run_id="r", policy=POLICY)
    assert not metrics.validity.baseline_healthy


def test_pairing_errors():
    scores = _scores()
    with pytest.raises(ValueError, match="missing a role"):
        pair_scores(scores[:-1])
    with pytest.raises(ValueError, match="two candidate"):
        pair_scores(scores + scores[-1:])
