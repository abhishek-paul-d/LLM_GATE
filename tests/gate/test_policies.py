"""Shipped policy files: every one loads, and policy_v2 is policy_v1 without cost limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from release_gate.gate import load_policy

POLICIES = Path(__file__).resolve().parents[2] / "policies"


@pytest.mark.parametrize("path", sorted(POLICIES.glob("*.yaml")), ids=lambda p: p.name)
def test_policy_files_load_and_match_their_file_name(path):
    policy = load_policy(path)
    assert path.stem == f"policy_v{policy.policy_version}"


def test_policy_v2_is_v1_without_cost():
    v1, v2 = load_policy(POLICIES / "policy_v1.yaml"), load_policy(POLICIES / "policy_v2.yaml")
    assert v1.cost is not None and v2.cost is None
    assert v2.model_dump(exclude={"policy_version", "cost"}) == v1.model_dump(exclude={"policy_version", "cost"})
