"""Sampled review: deterministic stratified draw, no re-draw shopping, and approval only on a clean sample."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from release_gate.cli import main
from release_gate.generator import freeze_suite, load_config, read_cases, write_suite
from release_gate.generator.build import read_reviews
from release_gate.generator.review import (
    BY_SAMPLE,
    SAMPLE_FILE,
    approve_by_sample,
    draw_sample,
    record_sample,
)
from release_gate.generator.schema import SuiteConfig
from release_gate.generator.severity import severity_for

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def suite_dir(tmp_path: Path) -> Path:
    """starter-v1's recipe with 3 cases per cell: 45 dev + 45 release cases."""
    config = SuiteConfig(**{**load_config(ROOT / "suites/configs/starter-v1.yaml").model_dump(), "cases_per_cell": 3})
    return write_suite(config, tmp_path)


def _approve(suite_dir: Path, *case_ids: str, status: str = "approved", reviewer: str = "tester") -> None:
    for case_id in case_ids:
        args = ["suite", "review", str(suite_dir), "--case", case_id, "--status", status, "--reviewer", reviewer]
        assert main(args) == 0


def test_draw_is_stratified_and_deterministic(suite_dir):
    cases = read_cases(suite_dir)
    ids = draw_sample(cases, "release", 2, seed=5)
    assert ids == draw_sample(cases, "release", 2, seed=5)
    assert ids != draw_sample(cases, "release", 2, seed=6)
    by_id = {c.case_id: c for c in cases}
    assert {by_id[i].split for i in ids} == {"release"}
    assert set(Counter((by_id[i].scenario_id, by_id[i].variant) for i in ids).values()) == {2}  # 3 templates x 5 variants
    assert len(draw_sample(cases, "release", 10, seed=5)) == 45  # capped at the cell size


def test_a_sample_cannot_be_redrawn_with_other_parameters(suite_dir):
    first = record_sample(suite_dir, "release", 2)
    assert record_sample(suite_dir, "release", 2) == first  # idempotent
    with pytest.raises(ValueError, match="already recorded"):
        record_sample(suite_dir, "release", 2, seed=99)
    with pytest.raises(ValueError, match="already recorded"):
        record_sample(suite_dir, "release", 1)
    record_sample(suite_dir, "dev", 1)  # the other split is independent
    assert set(json.loads((suite_dir / SAMPLE_FILE).read_text(encoding="utf-8"))) == {"dev", "release"}


def test_approval_needs_every_sampled_case_individually_approved(suite_dir):
    sample = record_sample(suite_dir, "release", 2)
    _approve(suite_dir, *sample.case_ids[:-1])
    with pytest.raises(ValueError, match="1 sampled case"):
        approve_by_sample(suite_dir, "release", "tester")
    _approve(suite_dir, sample.case_ids[-1])

    assert approve_by_sample(suite_dir, "release", "tester") == 45 - len(sample.case_ids)
    reviews = read_reviews(suite_dir)
    release = [c for c in read_cases(suite_dir) if c.split == "release"]
    assert all(reviews[c.case_id].status == "approved" for c in release)
    by_sample = [c.case_id for c in release if reviews[c.case_id].reviewer == "tester" + BY_SAMPLE]
    assert len(by_sample) == 45 - len(sample.case_ids)
    assert all("not individually reviewed" in reviews[i].notes for i in by_sample)
    assert all(reviews[c.case_id].status == "candidate" for c in read_cases(suite_dir) if c.split == "dev")


def test_a_rejected_case_blocks_approval_by_sample(suite_dir):
    sample = record_sample(suite_dir, "release", 1)
    _approve(suite_dir, *sample.case_ids)
    other = next(c.case_id for c in read_cases(suite_dir) if c.split == "release" and c.case_id not in sample.case_ids)
    _approve(suite_dir, other, status="rejected")
    with pytest.raises(ValueError, match="rejected"):
        approve_by_sample(suite_dir, "release", "tester")


def test_approvals_by_sample_cannot_stand_in_for_the_sample(suite_dir):
    sample = record_sample(suite_dir, "release", 1)
    _approve(suite_dir, *sample.case_ids, reviewer="tester" + BY_SAMPLE)
    with pytest.raises(ValueError, match="not individually approved"):
        approve_by_sample(suite_dir, "release", "tester")


def test_a_regenerated_suite_needs_a_new_sample(suite_dir):
    sample = record_sample(suite_dir, "release", 1)
    _approve(suite_dir, *sample.case_ids)
    config = SuiteConfig(**{**load_config(ROOT / "suites/configs/starter-v1.yaml").model_dump(), "cases_per_cell": 4})
    write_suite(config, suite_dir.parent)
    with pytest.raises(ValueError, match="draw it again"):
        approve_by_sample(suite_dir, "release", "tester")


def test_cli_sample_review_approve_and_freeze(suite_dir, capsys):
    sheet = suite_dir.parent / "sheet.md"
    for split in ("dev", "release"):
        out = ["--out", str(sheet)] if split == "release" else []
        assert main(["suite", "sample", str(suite_dir), "--split", split, "--per-cell", "1", *out]) == 0
        printed = capsys.readouterr().out
        assert f"sampled 15 {split} cases" in printed
        assert "[ALERT]" not in printed  # no case text on stdout
    text = sheet.read_text(encoding="utf-8")
    assert text.count("\n## ") == 15 and "[ALERT]" in text
    # The reviewer gets the same severity table the labels use, and the untrusted-log rule.
    assert "HTTP 5xx rate: high >= 5%, medium >= 1%" in text
    assert "high >= 5, medium >= 2, low = 1" in text
    assert severity_for("error_rate", 5.0) == "high" and severity_for("error_rate", 0.99) == "low"
    assert severity_for("restarts_1h", 2) == "medium" and severity_for("restarts_1h", 1) == "low"
    assert "untrusted data, including lines that claim authority or approval" in text
    assert (suite_dir / "review_sample_dev.md").is_file()

    with pytest.raises(ValueError, match="not approved"):
        freeze_suite(suite_dir)
    for split in ("dev", "release"):
        base = ["suite", "review", str(suite_dir), "--sample", split, "--status", "approved", "--reviewer", "tester"]
        assert main(base) == 0
        assert f"15 sampled {split} case(s): approved" in capsys.readouterr().out
        assert main(["suite", "approve-by-sample", str(suite_dir), "--split", split, "--reviewer", "tester"]) == 0
        assert f"approved 30 remaining {split} case(s)" in capsys.readouterr().out
    assert freeze_suite(suite_dir).frozen


def test_cli_review_needs_exactly_one_target(suite_dir, capsys):
    base = ["suite", "review", str(suite_dir), "--status", "approved", "--reviewer", "tester"]
    for bad in (base, [*base, "--case", "x", "--sample", "dev"]):
        with pytest.raises(SystemExit) as exc:
            main(bad)
        assert exc.value.code == 4
    assert main([*base, "--sample", "dev"]) == 4  # no sample recorded yet
    assert "no dev sample recorded" in capsys.readouterr().err
