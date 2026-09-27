"""The single severity rule: severity follows from the symptom's impact, never from the cause.

Generation samples a target severity first (to balance labels), then a symptom value inside
that band. Validation re-derives severity from the stored symptom with the same table, so a
hand-edited label or value that disagrees is caught.
"""

from __future__ import annotations

from typing import Literal

Severity = Literal["low", "medium", "high"]
SymptomKind = Literal["error_rate", "restarts_1h", "unavailable_replicas"]
SEVERITIES: tuple[Severity, ...] = ("low", "medium", "high")

# error_rate: percent of requests failing. restarts_1h: container restarts in the last hour.
# unavailable_replicas: unavailable / desired replicas (limit = desired).
ERROR_RATE_BANDS = {"low": (0.2, 0.9), "medium": (1.0, 4.9), "high": (5.0, 40.0)}
RESTART_BANDS = {"low": (1, 1), "medium": (2, 4), "high": (5, 30)}


def severity_for(kind: SymptomKind, value: float, limit: float | None = None, recovered: bool = False) -> Severity:
    if recovered:
        return "low"
    if kind == "error_rate":
        return "high" if value >= 5.0 else "medium" if value >= 1.0 else "low"
    if kind == "restarts_1h":
        return "high" if value >= 5 else "medium" if value >= 2 else "low"
    if kind == "unavailable_replicas":
        if not limit:
            raise ValueError("unavailable_replicas needs the desired replica count as limit")
        ratio = value / limit
        return "high" if ratio >= 1.0 else "medium" if ratio >= 0.5 else "low"
    raise ValueError(f"unknown symptom kind {kind!r}")
