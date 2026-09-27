import json
from pathlib import Path

from release_gate.gate import evaluate, load_policy
from release_gate.gate.models import RunMetrics
from release_gate.report import render_markdown

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/gate/pass_metrics.json"
POLICY = ROOT / "policies/policy_v1.yaml"
LIMITATIONS = (
    "Results were measured on a versioned synthetic benchmark. They describe behaviour on that benchmark only and do not "
    "prove production quality or safety. Absolute limits compare the candidate's point estimate; comparative limits use the "
    "paired confidence interval of (candidate - baseline)."
)


def inputs() -> tuple[RunMetrics, object]:
    metrics = RunMetrics.model_validate_json(FIXTURE.read_text(encoding="utf-8"))
    policy = load_policy(POLICY)
    return metrics, policy


def test_renders_complete_deterministic_pass_report() -> None:
    metrics, policy = inputs()
    decision = evaluate(metrics, policy)

    report = render_markdown(decision, metrics)

    assert report.startswith("# Release report: fixture-pass")
    assert "**Recommendation: PROMOTE**" in report
    assert all(rule.rule_id in report for rule in decision.rules)
    assert LIMITATIONS in report
    assert "| long_input | no |" in report
    assert "| prompt_injection | yes |" in report
    assert render_markdown(decision, metrics) == report


def test_rule_table_shows_condition_and_units() -> None:
    raw_metrics = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw_metrics["serving"]["p95_latency_ms"].update(baseline=1500, candidate=3000, delta_ci=[1300, 1700])
    metrics = RunMetrics.model_validate(raw_metrics)

    report = render_markdown(evaluate(metrics, load_policy(POLICY)), metrics)

    assert "| FAIL | serving.p95_regression | <= 25% | 100% | [86.67, 113.3]% |" in report
    assert "| PASS | serving.p95_latency_ms | <= 4000 ms | 3000 ms | - |" in report
    assert "| PASS | quality.accuracy_drop | >= -0.02 | 0.03 | [0.005, 0.055] |" in report


def test_reject_report_lists_triggered_serving_rule() -> None:
    raw_metrics = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw_metrics["serving"]["error_rate"]["candidate"] = 0.03
    metrics = RunMetrics.model_validate(raw_metrics)
    decision = evaluate(metrics, load_policy(POLICY))

    report = render_markdown(decision, metrics)

    assert "**Recommendation: REJECT**" in report
    triggered_section = report.split("## Triggered rules", maxsplit=1)[1].split("## All rules", maxsplit=1)[0]
    assert "serving.error_rate" in triggered_section
