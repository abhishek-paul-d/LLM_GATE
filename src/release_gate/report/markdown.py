"""Render deterministic Markdown release reports."""

from collections.abc import Mapping
from typing import Any

from jinja2 import Environment, PackageLoader, StrictUndefined

from release_gate.gate.models import Comparison, Decision, RuleResult, RunMetrics


def _format_number(value: float | int | None) -> str:
    return "-" if value is None else format(value, ".4g")


def _format_ci(interval: tuple[float, float] | None) -> str:
    if interval is None:
        return "-"
    return f"[{_format_number(interval[0])}, {_format_number(interval[1])}]"


def _with_unit(text: str, unit: str | None) -> str:
    if text == "-" or unit is None:
        return text
    return f"{text}%" if unit == "%" else f"{text} {unit}"


def _format_limit(rule: RuleResult) -> str:
    limit = _with_unit(_format_number(rule.limit), rule.unit)
    return limit if rule.direction is None or limit == "-" else f"{rule.direction} {limit}"


def _metric_rows(section: Mapping[str, Comparison]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "baseline": _format_number(comparison.baseline),
            "candidate": _format_number(comparison.candidate),
            "delta": _format_number(comparison.delta),
            "delta_ci": _format_ci(comparison.delta_ci),
            "n": comparison.n,
        }
        for name, comparison in sorted(section.items())
    ]


def render_markdown(decision: Decision, metrics: RunMetrics) -> str:
    """Render the decision and measured metrics as stable Markdown."""
    environment = Environment(
        loader=PackageLoader("release_gate.report", "templates"),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    template = environment.get_template("report.md.j2")

    triggered_by_id = {rule.rule_id: rule for rule in decision.rules}
    triggered_rules = [{"rule_id": rule_id, "message": triggered_by_id[rule_id].message} for rule_id in decision.triggered_rules]
    rule_rows = [
        {
            "status": rule.status.value.upper(),
            "rule_id": rule.rule_id,
            "limit": _format_limit(rule),
            "observed": _with_unit(_format_number(rule.observed), rule.unit),
            "ci": _with_unit(_format_ci(rule.ci), rule.unit),
            "n": "-" if rule.n is None else str(rule.n),
            "evidence": rule.evidence or "-",
        }
        for rule in decision.rules
    ]
    sections = [
        {"name": name.title(), "rows": _metric_rows(section)}
        for name in ("quality", "safety", "serving", "cost")
        if (section := getattr(metrics, name))
    ]
    protected_rules = {rule.rule_id for rule in decision.rules}
    slice_rows = []
    for name, slice_metrics in sorted(metrics.slices.items()):
        comparison = slice_metrics.get("accuracy")
        if comparison is None:
            continue
        slice_rows.append(
            {
                "name": name,
                "protected": "yes" if f"slice.{name}.accuracy_drop" in protected_rules else "no",
                "baseline": _format_number(comparison.baseline),
                "candidate": _format_number(comparison.candidate),
                "delta": _format_number(comparison.delta),
                "delta_ci": _format_ci(comparison.delta_ci),
                "n": comparison.n,
            }
        )

    run_fields = [
        ("Run ID", metrics.run_id),
        ("Mode", metrics.mode),
        ("Policy version", decision.policy_version),
        ("Gate version", decision.gate_version),
        ("Stats method", metrics.stats.method),
        ("Confidence level", _format_number(metrics.stats.confidence_level)),
        ("Bootstrap resamples", metrics.stats.resamples),
        ("Bootstrap seed", metrics.stats.seed),
    ]
    return template.render(
        run_id=metrics.run_id,
        outcome=decision.outcome.value,
        reason=decision.reason,
        run_fields=run_fields,
        triggered_rules=triggered_rules,
        rules=rule_rows,
        sections=sections,
        slices=slice_rows,
    )
