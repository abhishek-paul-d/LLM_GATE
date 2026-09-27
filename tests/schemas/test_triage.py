import json

import pytest
from pydantic import ValidationError

from release_gate.schemas import TriageRecord, parse_triage_record
from release_gate.schemas.triage import triage_json_schema


def valid_payload() -> dict[str, str]:
    return {
        "category": "capacity",
        "severity": "medium",
        "summary": "Queue depth increased.",
        "next_check": "Check worker capacity.",
    }


def test_valid_record_parses() -> None:
    record = parse_triage_record(json.dumps(valid_payload()))

    assert record == TriageRecord(**valid_payload())


@pytest.mark.parametrize("category", ["other", "CAPACITY", ""])
def test_invalid_category_is_rejected(category: str) -> None:
    payload = valid_payload() | {"category": category}

    with pytest.raises(ValidationError):
        parse_triage_record(json.dumps(payload))


@pytest.mark.parametrize("severity", ["critical", "MEDIUM", ""])
def test_invalid_severity_is_rejected(severity: str) -> None:
    payload = valid_payload() | {"severity": severity}

    with pytest.raises(ValidationError):
        parse_triage_record(json.dumps(payload))


def test_unknown_extra_field_is_rejected() -> None:
    payload = valid_payload() | {"extra": "not allowed"}

    with pytest.raises(ValidationError):
        parse_triage_record(json.dumps(payload))


def test_empty_summary_is_rejected() -> None:
    payload = valid_payload() | {"summary": ""}

    with pytest.raises(ValidationError):
        parse_triage_record(json.dumps(payload))


def test_summary_over_400_characters_is_rejected() -> None:
    payload = valid_payload() | {"summary": "x" * 401}

    with pytest.raises(ValidationError):
        parse_triage_record(json.dumps(payload))


def test_next_check_over_200_characters_is_rejected() -> None:
    payload = valid_payload() | {"next_check": "x" * 201}

    with pytest.raises(ValidationError):
        parse_triage_record(json.dumps(payload))


def test_leading_and_trailing_whitespace_is_stripped() -> None:
    payload = valid_payload() | {"summary": "  queue depth increased  ", "next_check": "  check workers  "}

    record = parse_triage_record(json.dumps(payload))

    assert record.summary == "queue depth increased"
    assert record.next_check == "check workers"


@pytest.mark.parametrize(
    "raw",
    [
        '```json\n{"category":"capacity","severity":"medium","summary":"x","next_check":"y"}\n```',
        'Alert: {"category":"capacity","severity":"medium","summary":"x","next_check":"y"}',
        "this is not JSON",
    ],
)
def test_parser_rejects_wrapped_or_non_json_text(raw: str) -> None:
    with pytest.raises(ValidationError):
        parse_triage_record(raw)


def test_json_schema_has_expected_properties() -> None:
    schema = triage_json_schema()

    assert isinstance(schema, dict)
    assert set(schema["properties"]) == {"category", "severity", "summary", "next_check"}
