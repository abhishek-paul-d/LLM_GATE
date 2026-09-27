"""Mock model: deterministic, schema-valid, and each persona degrades the way it claims."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from release_gate.generator import generate_cases, load_config
from release_gate.mock import PERSONAS, MockBackend, triage
from release_gate.schemas.triage import parse_triage_record

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def cases():
    return generate_cases(load_config(ROOT / "suites/configs/starter-v1.yaml"))


def test_reference_is_valid_deterministic_and_gets_severity_right(cases):
    for c in cases:
        text = triage(c.input_text, PERSONAS["reference"])
        assert text == triage(c.input_text, PERSONAS["reference"])
        record = parse_triage_record(text)
        assert record.severity == c.expected.severity, c.case_id
        assert record.next_check.startswith("kubectl describe")


def test_reference_mostly_right_and_ignores_injections(cases):
    right = sum(parse_triage_record(triage(c.input_text, PERSONAS["reference"])).category == c.expected.category for c in cases)
    assert right / len(cases) >= 0.7
    for c in (c for c in cases if c.injection):
        record = parse_triage_record(triage(c.input_text, PERSONAS["reference"]))
        assert getattr(record, c.injection.target_field) != c.injection.target_value


def test_obedient_follows_injections(cases):
    for c in (c for c in cases if c.injection):
        record = parse_triage_record(triage(c.input_text, PERSONAS["obedient"]))
        assert getattr(record, c.injection.target_field) == c.injection.target_value, c.case_id


def test_degraded_personas_degrade(cases):
    unsafe = [parse_triage_record(triage(c.input_text, PERSONAS["unsafe"])).next_check for c in cases]
    assert any("rollout restart" in n for n in unsafe)
    broken = [triage(c.input_text, PERSONAS["broken-json"]) for c in cases]
    assert any(not t.startswith("{") for t in broken)
    ref = [triage(c.input_text, PERSONAS["reference"]) for c in cases]
    reg = [triage(c.input_text, PERSONAS["regressed-quality"]) for c in cases]
    assert sum(a != b for a, b in zip(ref, reg, strict=True)) >= 3


def _request(model: str, alert: str) -> bytes:
    return json.dumps({"model": model, "messages": [{"role": "user", "content": alert}]}).encode()


def test_backend_routes_models_and_transient_errors(cases):
    backend = MockBackend(PERSONAS["flaky"].model_copy(update={"transient_error_rate": 1.0}), "served")
    assert backend.handle("GET", "/v1/models", b"")[1]["data"][0]["id"] == "served"
    assert backend.handle("POST", "/v1/chat/completions", _request("other", "x"))[0] == 404
    body = _request("served", cases[0].input_text)
    assert backend.handle("POST", "/v1/chat/completions", body)[0] == 500
    status, payload, _ = backend.handle("POST", "/v1/chat/completions", body)
    assert status == 200 and payload["choices"][0]["message"]["content"].startswith("{")
    assert backend.handle("POST", "/v1/chat/completions", b"{broken")[0] == 400
