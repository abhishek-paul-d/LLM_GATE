"""Schema for the synthetic alert triage workload."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["capacity", "configuration", "dependency", "unknown"]
Severity = Literal["low", "medium", "high"]


class TriageRecord(BaseModel):
    """Structured triage of one synthetic alert, as returned by the model under test."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    category: Category
    severity: Severity
    summary: str = Field(min_length=1, max_length=400)
    next_check: str = Field(min_length=1, max_length=200)


def parse_triage_record(raw: str) -> TriageRecord:
    """Parse raw model JSON without repairing it; malformed output raises ValidationError."""
    return TriageRecord.model_validate_json(raw)


def triage_json_schema() -> dict:
    """Return the JSON Schema for a triage record."""
    return TriageRecord.model_json_schema()
