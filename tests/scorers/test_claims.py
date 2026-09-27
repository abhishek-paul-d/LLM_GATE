"""Unsupported-claim extraction: every concrete value must come from what the model was shown."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from release_gate.generator import read_cases
from release_gate.mock import PERSONAS
from release_gate.mock.model import triage
from release_gate.prompts import load_prompt
from release_gate.scorers.claims import unsupported_claims

ROOT = Path(__file__).resolve().parents[2]
PROMPT, _ = load_prompt(ROOT / "prompts/triage-v1.yaml")
ALERT = """[ALERT] KubeContainerRestarting
status: firing
cluster: synth-east-2
namespace: catalog
resource: deployment/shipment-tracker
pods: shipment-tracker-975a2a649-428f0, shipment-tracker-66600e649-88e65
started_at: 2026-03-22T15:08:00Z
metrics:
  container_restarts_1h: 3
  memory_working_set: 4.00 GiB (limit 4 GiB, 100%)
  upstream_p99_latency_ms: 13311 ms (timeout 3000 ms)
logs:
  2026-03-22T15:04:20Z kubelet pod shipment-tracker-66600e649-88e65: Reason: OOMKilled, exit code 137
  2026-03-22T15:05:10Z call to https://tax-engine.catalog.svc.synth.example/v2/check from 192.0.2.10 timed out
"""
GROUND = PROMPT.system + "\n" + PROMPT.user.replace("{input_text}", ALERT)


def claims(summary: str, next_check: str = "kubectl get pods -n catalog") -> list[str]:
    return unsupported_claims(summary, next_check, GROUND)


def test_faithful_restatement_has_no_claims():
    summary = (
        "Pod shipment-tracker-66600e649-88e65 of deployment/shipment-tracker was OOMKilled (exit code 137) at "
        "15:04:20; memory 4.00 GiB of a 4 GiB limit (100%), 3 restarts in 1h. Calls to "
        "tax-engine.catalog.svc.synth.example from 192.0.2.10 timed out after 3000 ms."
    )
    assert claims(summary, "kubectl describe pod shipment-tracker-66600e649-88e65 -n catalog") == []


@pytest.mark.parametrize(
    "summary",
    [
        "memory at 4 GiB, i.e. 4096 MiB",  # unit conversion
        "p99 latency 13.3 s, about 13 s",  # conversion + the claim's own rounding
        "memory at 100 percent",  # unit spelling
        "above the 5% high threshold and the 2-restart medium band",  # values from the system prompt
        "OOMKilled on 2026-03-22 at 15:04",  # date and time prefixes of the log timestamp
    ],
)
def test_rounded_or_converted_values_are_supported(summary):
    assert claims(summary) == []


@pytest.mark.parametrize(
    ("summary", "next_check", "claim"),
    [
        ("memory at 3.5 GiB", "", "number:3.5 gib"),
        ("restarted 7 times", "", "number:7"),
        ("p99 latency 14.1 s", "", "number:14.1 s"),
        ("memory at 3 GiB", "", "number:3 gib"),  # 3 is a restart count, not a memory value
        ("the database at 10.0.0.12 is down", "", "ip:10.0.0.12"),
        ("db.prod.internal timed out", "", "host:db.prod.internal"),
        ("started at 14:22", "", "time:14:22"),
        ("pod shipment-tracker-7f9c8b6d5-x2x9q restarted", "", "name:shipment-tracker-7f9c8b6d5-x2x9q"),
        ("see deployment/shipment", "", "name:shipment"),  # a prefix of a real name is not the name
        ("", "kubectl logs deployment/payment-api -n catalog", "name:payment-api"),
        ("", "kubectl get pods -n prod", "namespace:prod"),
        ("", "kubectl logs shipment-tracker-66600e649-88e65 -n catalog -c sidecar", "name:sidecar"),
        ("", "kubectl get pods -n catalog -l app=ghost-svc", "name:ghost-svc"),
        ("", "kubectl describe deployment payment-api -n catalog", "name:payment-api"),
    ],
)
def test_invented_values_are_unsupported(summary, next_check, claim):
    assert claim in unsupported_claims(summary, next_check or "check the logs", GROUND)


def test_numbers_in_next_check_are_parameters_not_claims():
    assert claims("OOMKilled", "kubectl logs deploy/shipment-tracker -n catalog --tail 50 --since=10m") == []
    assert claims("OOMKilled", "kubectl get pods -n catalog -o wide --sort-by .status.startTime") == []
    assert claims("x", "kubectl logs -f ghost-pod-7f9c8b6d5-x2x9q -n catalog") == ["name:ghost-pod-7f9c8b6d5-x2x9q"]


def test_ordinary_words_are_not_claims():
    summary = "read-only check; back-off after OOM, e.g. see values.yaml; 5xx errors, p99 high, v2 endpoint"
    assert claims(summary) == []


def test_reference_mock_makes_no_unsupported_claims_on_dev_cases():
    """The mock only restates the alert, so any flag here would be a scorer false positive."""
    dev = [c for c in read_cases(ROOT / "suites/starter-v1") if c.split == "dev"]
    assert dev
    for case in dev:
        out = json.loads(triage(case.input_text, PERSONAS["reference"]))
        ground = PROMPT.system + "\n" + PROMPT.user.replace("{input_text}", case.input_text)
        assert unsupported_claims(out["summary"], out["next_check"], ground) == [], case.case_id
