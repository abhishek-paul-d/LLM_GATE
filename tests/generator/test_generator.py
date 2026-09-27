"""Generator: determinism, label invariants, leakage rules, and the freeze/review workflow."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from release_gate.generator import (
    FrozenSuiteError,
    freeze_suite,
    generate_cases,
    load_config,
    read_cases,
    validate_cases,
    validate_suite_dir,
    write_suite,
)
from release_gate.generator.build import REVIEW_FILE, case_sha256
from release_gate.generator.families import FAMILIES
from release_gate.generator.rng import Rng, derive_seed
from release_gate.generator.scenario import GENERIC_SYMPTOM_PHRASINGS, SYMPTOM_ALERTS
from release_gate.generator.schema import VARIANTS, SuiteCase, SuiteConfig
from release_gate.generator.validate import cross_split_similarity, validate_case
from release_gate.generator.variants import INJECTION_PHRASINGS

ROOT = Path(__file__).resolve().parents[2]
STARTER = ROOT / "suites" / "configs" / "starter-v1.yaml"


def _config(**overrides) -> SuiteConfig:
    return SuiteConfig(**{**load_config(STARTER).model_dump(), **overrides})


@pytest.fixture(scope="module")
def probe() -> list[SuiteCase]:
    """Every variant, several cases per cell: exercises all code paths."""
    return generate_cases(_config(suite_version="probe", variants=list(VARIANTS), cases_per_cell=4))


def _codes(issues) -> set[str]:
    return {i.code for i in issues}


# --------------------------------------------------------------------------- determinism


def test_rng_is_stable_across_python_versions():
    # Golden values: if these change, every published suite would silently change too.
    assert derive_seed(20260927, "memory_pressure/a", "clear", 0) == 8796926393301362350
    r = Rng(42)
    assert [r.below(100) for _ in range(5)] == [63, 2, 27, 22, 73]
    assert Rng(7).hex(8) == "52a18508"


def test_same_seed_is_byte_identical(tmp_path):
    a = write_suite(_config(), tmp_path / "a")
    b = write_suite(_config(), tmp_path / "b")
    for name in ("cases.jsonl", "manifest.json", "review.json"):
        assert (a / name).read_bytes() == (b / name).read_bytes(), name


def test_different_seed_changes_cases():
    a = generate_cases(_config())
    b = generate_cases(_config(seed=1))
    assert [c.input_text for c in a] != [c.input_text for c in b]
    assert [c.case_id for c in a] == [c.case_id for c in b]


# --------------------------------------------------------------------------- shipped starter suite


def test_starter_config_shape():
    cases = generate_cases(load_config(STARTER))
    assert len(cases) == 30
    assert Counter(c.split for c in cases) == {"dev": 15, "release": 15}
    assert {c.variant for c in cases} == {"clear", "recovered", "missing_evidence", "conflicting", "prompt_injection"}
    assert validate_cases(cases) == []


def test_committed_starter_suite_matches_generator():
    suite_dir = ROOT / "suites" / "starter-v1"
    assert validate_suite_dir(suite_dir) == []
    assert [c.model_dump() for c in read_cases(suite_dir)] == [c.model_dump() for c in generate_cases(load_config(STARTER))]


def test_probe_suite_is_valid(probe):
    assert validate_cases(probe) == []


# --------------------------------------------------------------------------- labels


def test_labels_follow_variants(probe):
    for c in probe:
        e = c.expected
        if c.variant in ("missing_evidence", "conflicting"):
            assert (e.category, e.behavior) == ("unknown", "ask_for_signal"), c.case_id
        else:
            assert e.category != "unknown" and e.acceptable_categories == [e.category], c.case_id
        if c.variant == "recovered":
            assert e.severity == "low"
        if c.variant == "conflicting":
            assert len(e.acceptable_categories) == 3 and e.acceptable_categories[0] == "unknown"


def test_categories_are_balanced(probe):
    counts = Counter(c.expected.category for c in probe if c.expected.category != "unknown")
    assert len(set(counts.values())) == 1, counts


def test_injection_never_matches_the_label(probe):
    for c in (c for c in probe if c.injection):
        label = c.expected.severity if c.injection.target_field == "severity" else c.expected.category
        assert c.injection.target_value != label


# --------------------------------------------------------------------------- leakage


def test_no_scenario_in_both_splits(probe):
    splits: dict[str, set[str]] = {}
    for c in probe:
        splits.setdefault(c.scenario_id, set()).add(c.split)
    assert all(len(s) == 1 for s in splits.values())


def test_split_specific_phrasing_does_not_cross(probe):
    def stems(pool):
        return [p.split("{")[0].strip() for p in pool if p.split("{")[0].strip()]

    for c in probe:
        other = "release" if c.split == "dev" else "dev"
        foreign = stems(INJECTION_PHRASINGS[other])
        for kind in GENERIC_SYMPTOM_PHRASINGS[other].values():
            foreign += stems(kind)
        leaked = [s for s in foreign if len(s) > 12 and s in c.input_text]
        assert not leaked, (c.case_id, leaked)


def test_cross_split_similarity_has_margin(probe):
    top, dev_case, rel_case = cross_split_similarity(probe)[0]
    assert top < 0.7, (top, dev_case, rel_case)


def test_conflicting_needs_two_categories_per_split():
    with pytest.raises(ValueError, match="two categories"):
        generate_cases(_config(release_templates=["memory_pressure/b"]))


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"families": ["nope"]}, "unknown families"),
        ({"release_templates": ["memory_pressure/z"]}, "not in the selected families"),
        ({"variants": ["clear", "clear"]}, "must not repeat"),
    ],
)
def test_bad_configs_rejected(override, match):
    with pytest.raises(ValueError, match=match):
        generate_cases(_config(**override))


# --------------------------------------------------------------------------- invariants catch corruption


def _one(probe, variant="clear", split="dev") -> SuiteCase:
    return next(c for c in probe if c.variant == variant and c.split == split)


def _with_expected(c: SuiteCase, **changes) -> SuiteCase:
    return c.model_copy(update={"expected": c.expected.model_copy(update=changes)})


def test_mutations_are_caught(probe):
    clear = _one(probe)
    inj = _one(probe, "prompt_injection")
    missing = _one(probe, "missing_evidence")
    text = clear.input_text
    other_sev = next(s for s in ("low", "medium", "high") if s != clear.expected.severity)
    mutations = {
        "V02": clear.model_copy(update={"entities": [*clear.entities, "phantom-svc"]}),
        "V03": _with_expected(clear, required_facts=[*clear.expected.required_facts, ["never mentioned"]]),
        "V04": _with_expected(clear, next_check_targets=[*clear.expected.next_check_targets, "ghost-db"]),
        "V05": _with_expected(missing, category="capacity", acceptable_categories=["capacity"]),
        "V06": _with_expected(clear, severity=other_sev),
        "V07": inj.model_copy(
            update={
                "injection": inj.injection.model_copy(
                    update={
                        "target_value": inj.expected.severity
                        if inj.injection.target_field == "severity"
                        else inj.expected.category
                    }
                )
            }
        ),  # noqa: E501
        "V08": clear.model_copy(update={"started_at": "2026-01-01T00:00:00Z"}),
        "V10": clear.model_copy(update={"input_text": text + "  note: see https://status.example.com or 8.8.8.8\n"}),
    }
    for code, case in mutations.items():
        assert code in _codes(validate_case(case)), code


def test_acceptable_categories_only_widen_for_conflicts(probe):
    clear = _one(probe)
    widened = _with_expected(clear, acceptable_categories=[clear.expected.category, "unknown"])
    assert "V05" in _codes(validate_case(widened))


@pytest.mark.parametrize(
    "snippet",
    ["contact oncall@corp.test", "password=hunter2", "AKIAABCDEFGHIJKLMNOP", "-----BEGIN RSA PRIVATE KEY-----"],
)
def test_sensitive_looking_strings_rejected(probe, snippet):
    c = _one(probe)
    assert "V10" in _codes(validate_case(c.model_copy(update={"input_text": c.input_text + snippet + "\n"})))


def test_url_hosts_must_be_synthetic(probe):
    c = _one(probe)

    def codes_with(line: str) -> set[str]:
        return _codes(validate_case(c.model_copy(update={"input_text": c.input_text + line + "\n"})))

    assert "V10" not in codes_with('Get "http://192.0.2.10:9090/ready"')
    assert "V10" in codes_with('Get "http://10.0.0.8:9090/ready"')
    assert "V10" in codes_with("see https://status.internal/ready")


def test_metric_percent_mismatch_caught(probe):
    c = next(c for c in probe if any(m.unit == "GiB" for m in c.metrics))
    metrics = [m.model_copy(update={"value": m.limit * 0.5}) if m.unit == "GiB" else m for m in c.metrics]
    assert "V09" in _codes(validate_case(c.model_copy(update={"metrics": metrics})))


def test_suite_level_checks(probe):
    a, b = probe[0], probe[1]
    assert "S01" in _codes(validate_cases([a, a.model_copy(update={"input_text": a.input_text + " "})]))
    moved = b.model_copy(update={"scenario_id": a.scenario_id, "split": "release" if a.split == "dev" else "dev"})
    assert "S02" in _codes(validate_cases([a, moved]))
    twin = a.model_copy(update={"case_id": "twin", "split": "release", "scenario_id": "x/y"})
    assert "S03" in _codes(validate_cases([a, twin]))


# --------------------------------------------------------------------------- review and freeze


def _approve_all(suite_dir: Path) -> None:
    path = suite_dir / REVIEW_FILE
    reviews = json.loads(path.read_text(encoding="utf-8"))
    for rec in reviews.values():
        rec.update(status="approved", reviewer="test")
    path.write_text(json.dumps(reviews), encoding="utf-8")


def test_freeze_requires_approval_and_blocks_regeneration(tmp_path):
    suite_dir = write_suite(_config(), tmp_path)
    with pytest.raises(ValueError, match="not approved"):
        freeze_suite(suite_dir)
    _approve_all(suite_dir)
    assert freeze_suite(suite_dir).frozen
    with pytest.raises(FrozenSuiteError):
        write_suite(_config(), tmp_path)


def test_regeneration_keeps_matching_reviews_and_resets_changed(tmp_path):
    suite_dir = write_suite(_config(), tmp_path)
    _approve_all(suite_dir)
    write_suite(_config(), tmp_path)  # unchanged cases keep their approval
    assert {r["status"] for r in json.loads((suite_dir / REVIEW_FILE).read_text()).values()} == {"approved"}
    write_suite(_config(seed=99), tmp_path)  # every case changed
    assert {r["status"] for r in json.loads((suite_dir / REVIEW_FILE).read_text()).values()} == {"candidate"}


def test_malformed_review_file_is_an_error_not_a_crash(tmp_path):
    suite_dir = write_suite(_config(), tmp_path)
    (suite_dir / REVIEW_FILE).write_text("[1, 2]", encoding="utf-8")
    assert _codes(validate_suite_dir(suite_dir)) == {"V01"}
    with pytest.raises(ValueError, match="must map case_id"):
        write_suite(_config(), tmp_path)  # never silently overwrite a review file it cannot read


def test_hand_edited_suite_is_detected(tmp_path):
    suite_dir = write_suite(_config(), tmp_path)
    _approve_all(suite_dir)
    cases_file = suite_dir / "cases.jsonl"
    lines = cases_file.read_text(encoding="utf-8").splitlines()
    first = SuiteCase.model_validate_json(lines[0])
    edited = first.model_copy(
        update={
            "expected": first.expected.model_copy(update={"severity": "high" if first.expected.severity != "high" else "low"})
        }
    )
    lines[0] = json.dumps(edited.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    cases_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    codes = _codes(validate_suite_dir(suite_dir))
    assert {"S04", "S05", "V06"} <= codes
    assert case_sha256(edited) != case_sha256(first)


# --------------------------------------------------------------------------- all families


@pytest.fixture(scope="module")
def probe_all() -> list[SuiteCase]:
    fams = list(FAMILIES)
    config = _config(
        suite_version="probe-all",
        families=fams,
        variants=list(VARIANTS),
        cases_per_cell=2,
        release_templates=[f"{f}/b" for f in fams],
    )
    return generate_cases(config)


def _alert(c: SuiteCase) -> str:
    return c.input_text.split("\n", 1)[0].removeprefix("[ALERT] ")


def test_all_families_valid_with_margin(probe_all):
    assert validate_cases(probe_all) == []
    assert cross_split_similarity(probe_all)[0][0] < 0.7


def test_missing_evidence_never_names_the_cause_in_the_alert(probe_all):
    templates = {t.scenario_id: t for f in FAMILIES.values() for t in f.templates}
    swapped = 0
    for c in probe_all:
        own = templates[c.scenario_id].alert_name
        if c.variant == "missing_evidence":
            assert _alert(c) in SYMPTOM_ALERTS[c.symptom.kind], c.case_id
            swapped += _alert(c) != own
        else:
            assert _alert(c) == own, c.case_id
    assert swapped > 0  # some templates (e.g. KubePersistentVolumeFillingUp) do name their cause


def test_cause_naming_alert_on_missing_evidence_is_caught(probe_all):
    c = next(c for c in probe_all if c.variant == "missing_evidence")
    text = c.input_text.replace(_alert(c), "KubePersistentVolumeFillingUp")
    assert "V05" in _codes(validate_case(c.model_copy(update={"input_text": text})))


def test_cases_per_variant_overrides_cell_count():
    cases = generate_cases(_config(cases_per_cell=1, cases_per_variant={"prompt_injection": 3}))
    counts = Counter(c.variant for c in cases)
    assert counts["prompt_injection"] == 3 * 6 and counts["clear"] == 6
    with pytest.raises(ValueError, match="cases_per_variant"):
        generate_cases(_config(cases_per_variant={"long_input": 2}))
