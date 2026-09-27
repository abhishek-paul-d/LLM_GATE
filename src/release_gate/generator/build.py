"""Assemble, write, and freeze suite versions."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import yaml

from .families import FAMILIES
from .rng import Rng, derive_seed
from .scenario import Template, base_draft
from .schema import (
    GENERATOR_VERSION,
    SCORING_RULES_VERSION,
    ReviewRecord,
    SuiteCase,
    SuiteConfig,
    SuiteManifest,
)
from .variants import apply_variant

CASES_FILE = "cases.jsonl"
MANIFEST_FILE = "manifest.json"
REVIEW_FILE = "review.json"


class FrozenSuiteError(RuntimeError):
    pass


def load_config(path: str | Path) -> SuiteConfig:
    with open(path, encoding="utf-8") as f:
        return SuiteConfig.model_validate(yaml.safe_load(f))


def _split_of(config: SuiteConfig) -> dict[str, str]:
    known = {t.scenario_id for name in config.families for t in FAMILIES[name].templates}
    unknown = sorted(set(config.release_templates) - known)
    if unknown:
        raise ValueError(f"release_templates not in the selected families: {unknown}")
    return {sid: ("release" if sid in config.release_templates else "dev") for sid in sorted(known)}


def _check_config(config: SuiteConfig) -> dict[str, str]:
    missing = [f for f in config.families if f not in FAMILIES]
    if missing:
        raise ValueError(f"unknown families {missing}; registered: {sorted(FAMILIES)}")
    if len(set(config.families)) != len(config.families) or len(set(config.variants)) != len(config.variants):
        raise ValueError("families and variants must not repeat")
    split_of = _split_of(config)
    if "conflicting" in config.variants:
        for split in sorted(set(split_of.values())):
            cats = {
                FAMILIES[f].category for f in config.families for t in FAMILIES[f].templates if split_of[t.scenario_id] == split
            }
            if len(cats) < 2:
                raise ValueError(f"'conflicting' needs templates from at least two categories in split {split!r}")
    return split_of


def case_line(case: SuiteCase) -> str:
    """Canonical JSON line; the byte-identity guarantee rests on this."""
    return json.dumps(case.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def case_sha256(case: SuiteCase) -> str:
    return hashlib.sha256(case_line(case).encode("utf-8")).hexdigest()


def generate_cases(config: SuiteConfig) -> list[SuiteCase]:
    split_of = _check_config(config)
    templates: list[tuple[str, Template]] = [(f, t) for f in config.families for t in FAMILIES[f].templates]
    cases: list[SuiteCase] = []
    for family_name, template in templates:
        family = FAMILIES[family_name]
        split = split_of[template.scenario_id]
        donors = [t for f, t in templates if split_of[t.scenario_id] == split and FAMILIES[f].category != family.category]
        for variant in config.variants:
            for i in range(config.cases_per_cell):
                seed = derive_seed(config.seed, template.scenario_id, variant, i)
                rng = Rng(seed)
                draft = apply_variant(base_draft(family, template, split, rng), variant, rng, donors)
                case_id = f"{template.scenario_id.replace('/', '-')}-{variant}-{i:03d}"
                cases.append(draft.to_case(case_id, config.suite_version, seed))
    return cases


def build_manifest(config: SuiteConfig, cases: list[SuiteCase], cases_bytes: bytes, frozen: bool = False) -> SuiteManifest:
    def count(key) -> dict[str, int]:
        return dict(sorted(Counter(key(c) for c in cases).items()))

    return SuiteManifest(
        suite_version=config.suite_version,
        generator_version=GENERATOR_VERSION,
        scoring_rules_version=SCORING_RULES_VERSION,
        seed=config.seed,
        config=config,
        n_cases=len(cases),
        counts={
            "split": count(lambda c: c.split),
            "family": count(lambda c: c.family),
            "variant": count(lambda c: c.variant),
            "category": count(lambda c: c.expected.category),
            "severity": count(lambda c: c.expected.severity),
            "behavior": count(lambda c: c.expected.behavior),
        },
        cases_sha256=hashlib.sha256(cases_bytes).hexdigest(),
        frozen=frozen,
    )


def read_manifest(suite_dir: str | Path) -> SuiteManifest:
    return SuiteManifest.model_validate_json((Path(suite_dir) / MANIFEST_FILE).read_text(encoding="utf-8"))


def read_cases(suite_dir: str | Path) -> list[SuiteCase]:
    text = (Path(suite_dir) / CASES_FILE).read_text(encoding="utf-8")
    return [SuiteCase.model_validate_json(line) for line in text.splitlines() if line]


def read_reviews(suite_dir: str | Path) -> dict[str, ReviewRecord]:
    path = Path(suite_dir) / REVIEW_FILE
    if not path.is_file():
        return {}
    return {k: ReviewRecord.model_validate(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")


def write_suite(config: SuiteConfig, out_root: str | Path) -> Path:
    """Generate and write ``<out_root>/<suite_version>/``. Refuses to touch a frozen suite.

    Existing reviews are kept for cases whose content is unchanged, and reset to candidate
    for new or changed cases, so a review always refers to the exact case it approved.
    """
    suite_dir = Path(out_root) / config.suite_version
    if (suite_dir / MANIFEST_FILE).is_file() and read_manifest(suite_dir).frozen:
        raise FrozenSuiteError(f"{suite_dir} is frozen; bump suite_version instead of regenerating it")
    cases = generate_cases(config)
    cases_bytes = "".join(case_line(c) + "\n" for c in cases).encode("utf-8")
    suite_dir.mkdir(parents=True, exist_ok=True)
    (suite_dir / CASES_FILE).write_bytes(cases_bytes)
    _write_json(suite_dir / MANIFEST_FILE, build_manifest(config, cases, cases_bytes).model_dump(mode="json"))
    old = read_reviews(suite_dir)
    reviews = {}
    for c in cases:
        h = case_sha256(c)
        prev = old.get(c.case_id)
        reviews[c.case_id] = prev if prev is not None and prev.case_sha256 == h else ReviewRecord(case_sha256=h)
    _write_json(suite_dir / REVIEW_FILE, {k: v.model_dump(mode="json") for k, v in reviews.items()})
    return suite_dir


def freeze_suite(suite_dir: str | Path) -> SuiteManifest:
    """Mark a suite frozen once it validates and every case is approved."""
    from .validate import validate_suite_dir

    suite_dir = Path(suite_dir)
    issues = validate_suite_dir(suite_dir)
    if issues:
        raise ValueError(f"cannot freeze {suite_dir}: {len(issues)} validation issue(s), first: {issues[0]}")
    reviews = read_reviews(suite_dir)
    pending = sorted(k for k, v in reviews.items() if v.status != "approved")
    if pending:
        raise ValueError(f"cannot freeze {suite_dir}: {len(pending)} case(s) not approved, e.g. {pending[:3]}")
    manifest = read_manifest(suite_dir).model_copy(update={"frozen": True})
    _write_json(suite_dir / MANIFEST_FILE, manifest.model_dump(mode="json"))
    return manifest
