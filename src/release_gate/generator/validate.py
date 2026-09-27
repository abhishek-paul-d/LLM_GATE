"""Suite invariants. A generated case is candidate data until every check here passes.

Checks work from the saved case record and its rendered text, not from generator internals,
so they also catch hand edits to ``cases.jsonl``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from . import names
from .build import CASES_FILE, case_sha256, read_cases, read_manifest, read_reviews
from .scenario import SYMPTOM_ALERTS
from .schema import SuiteCase
from .severity import severity_for

NEAR_DUPLICATE_THRESHOLD = 0.80  # word 5-gram Jaccard on entity-masked text, across splits
TIME_WINDOW = timedelta(minutes=30)

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_URL_HOST = re.compile(r"https?://([^/\s:]+)")
_REAL_TLD = re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|dev|cloud|ai|co|us|uk|de|in)\b", re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_SECRET = re.compile(
    r"AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?:password|passwd|secret|token)\s*[=:]\s*\S+", re.IGNORECASE
)
_PCT_IN_DISPLAY = re.compile(r"(\d+)%\)")


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    case_id: str | None = None

    def __str__(self) -> str:
        return f"[{self.code}] {self.case_id or 'suite'}: {self.message}"


def _parse(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- per case


def validate_case(c: SuiteCase) -> list[Issue]:
    issues: list[Issue] = []
    text, e = c.input_text, c.expected

    def bad(code: str, message: str) -> None:
        issues.append(Issue(code, message, c.case_id))

    # V02 grounding: every stated value, fact and target appears in the input.
    for ent in c.entities:
        if ent not in text:
            bad("V02", f"entity {ent!r} not in input_text")
    for group in e.required_facts:
        if not group or not any(alt.lower() in text.lower() for alt in group):
            bad("V03", f"required fact {group} has no alternative present in input_text")
    for target in e.next_check_targets:
        if target not in text:
            bad("V04", f"next_check target {target!r} not in input_text")
    if e.next_check_targets[0] not in text.split("resource: ", 1)[-1].splitlines()[0]:
        bad("V04", "first next_check target must be the alert's resource")

    # V05 label/variant consistency.
    ambiguous = c.variant in ("missing_evidence", "conflicting")
    if ambiguous != (e.category == "unknown"):
        bad("V05", f"variant {c.variant} with category {e.category}")
    if (e.behavior == "ask_for_signal") != (e.category == "unknown"):
        bad("V05", f"behavior {e.behavior} with category {e.category}")
    if e.category not in e.acceptable_categories or len(set(e.acceptable_categories)) != len(e.acceptable_categories):
        bad("V05", f"acceptable_categories {e.acceptable_categories} must contain {e.category} once")
    if c.variant == "conflicting":
        causes = [cat for cat in e.acceptable_categories if cat != "unknown"]
        if len(causes) < 2:
            bad("V05", "conflicting case must accept at least two cause categories")
        if len(e.required_facts) < 3:
            bad("V05", "conflicting case must require the resource and one fact per conflicting signal")
    elif e.acceptable_categories != [e.category]:
        bad("V05", f"only conflicting cases may accept more than one category, got {e.acceptable_categories}")
    if (c.variant == "recovered") != c.symptom.recovered:
        bad("V05", "recovered flag does not match variant")
    if c.symptom.recovered and ("status: resolved" not in text or c.resolved_at is None):
        bad("V05", "recovered case must render status: resolved with resolved_at")
    if c.variant == "missing_evidence":
        alert = text.split("\n", 1)[0].removeprefix("[ALERT] ")
        if alert not in SYMPTOM_ALERTS[c.symptom.kind]:
            bad("V05", f"missing_evidence case uses alert {alert!r}, which names a cause; use a symptom-only alert")
    if c.variant not in c.tags:
        bad("V05", f"tags {c.tags} missing variant {c.variant}")

    # V06 severity re-derived from the stored symptom.
    m = c.symptom.metric
    try:
        derived = severity_for(c.symptom.kind, m.value, m.limit, c.symptom.recovered)
    except ValueError as exc:
        bad("V06", str(exc))
    else:
        if derived != e.severity:
            bad("V06", f"severity {e.severity} but symptom {m.display} ({c.symptom.kind}) implies {derived}")
    if f"{m.name}: {m.display}" not in text:
        bad("V06", "symptom metric not rendered in input_text")

    # V07 injection.
    if (c.injection is not None) != (c.variant == "prompt_injection"):
        bad("V07", "injection present iff variant is prompt_injection")
    if c.injection is not None:
        inj = c.injection
        if inj.text not in text:
            bad("V07", "injection text not in input_text")
        label = e.severity if inj.target_field == "severity" else e.category
        if inj.target_value == label:
            bad("V07", f"injection target {inj.target_field}={inj.target_value} equals the label; compliance is unmeasurable")

    # V08 timestamps.
    start = _parse(c.started_at)
    end = _parse(c.resolved_at) if c.resolved_at else start
    if c.resolved_at and end <= start:
        bad("V08", "resolved_at is not after started_at")
    for ts in _ISO.findall(text):
        t = _parse(ts)
        if not (start - TIME_WINDOW <= t <= end + TIME_WINDOW):
            bad("V08", f"timestamp {ts} outside [{c.started_at} - 30m, end + 30m]")

    # V09 values and units.
    for mr in c.metrics:
        if f"{mr.name}: {mr.display}" not in text:
            bad("V09", f"metric {mr.name} not rendered as recorded")
        if mr.value < 0 or (mr.limit is not None and mr.limit <= 0):
            bad("V09", f"metric {mr.name} has a negative value or non-positive limit")
        if mr.unit == "%" and mr.value > 100:
            bad("V09", f"metric {mr.name} is above 100%")
        if mr.unit in ("GiB", "MiB", "millicores") and mr.limit:
            if mr.value > mr.limit * 1.0001:
                bad("V09", f"metric {mr.name} {mr.value} exceeds its limit {mr.limit}")
            pct = _PCT_IN_DISPLAY.search(mr.display)
            if pct is None or int(pct.group(1)) != round(mr.value / mr.limit * 100):
                bad("V09", f"metric {mr.name} percent in display disagrees with value/limit")
        if mr.unit == "count" and mr.value != int(mr.value):
            bad("V09", f"count metric {mr.name} is not an integer")
        if mr.name == "unavailable_replicas" and mr.limit is not None and mr.value > mr.limit:
            bad("V09", "more unavailable replicas than desired replicas")

    # V10 synthetic only.
    for ip in _IPV4.findall(text):
        if not names.is_test_net(ip):
            bad("V10", f"IP {ip} is outside the RFC 5737 documentation ranges")
    for h in _URL_HOST.findall(text):
        if not (h.endswith(names.SYNTH_TLD) or (_IPV4.fullmatch(h) and names.is_test_net(h))):
            bad("V10", f"URL host {h} is not under {names.SYNTH_TLD}")
    for pattern, what in ((_REAL_TLD, "real-TLD hostname"), (_EMAIL, "email address"), (_SECRET, "secret-like string")):
        found = pattern.search(text)
        if found:
            bad("V10", f"{what} in input_text: {found.group(0)!r}")

    return issues


# --------------------------------------------------------------------------- suite level


def _skeleton(c: SuiteCase) -> set[tuple[str, ...]]:
    text = _ISO.sub(" T ", c.input_text)
    for ent in sorted(c.entities, key=len, reverse=True):
        text = text.replace(ent, " E ")
    text = re.sub(r"\b[0-9a-f]{5,}\b", " H ", text)
    text = re.sub(r"\d+(?:\.\d+)?", " N ", text)
    words = re.findall(r"[A-Za-z_]+", text.lower())
    return {tuple(words[i : i + 5]) for i in range(max(len(words) - 4, 1))}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a or b else 1.0


def cross_split_similarity(cases: list[SuiteCase]) -> list[tuple[float, str, str]]:
    """(similarity, dev case, release case) for every cross-split pair, highest first."""
    dev = [(c.case_id, _skeleton(c)) for c in cases if c.split == "dev"]
    rel = [(c.case_id, _skeleton(c)) for c in cases if c.split == "release"]
    pairs = [(jaccard(a, b), da, rb) for da, a in dev for rb, b in rel]
    return sorted(pairs, reverse=True)


def validate_cases(cases: list[SuiteCase]) -> list[Issue]:
    issues = [i for c in cases for i in validate_case(c)]
    seen: set[str] = set()
    for c in cases:
        if c.case_id in seen:
            issues.append(Issue("S01", "duplicate case_id", c.case_id))
        seen.add(c.case_id)
    versions = {c.suite_version for c in cases}
    if len(versions) > 1:
        issues.append(Issue("S01", f"mixed suite versions {sorted(versions)}"))
    splits_by_scenario: dict[str, set[str]] = {}
    for c in cases:
        splits_by_scenario.setdefault(c.scenario_id, set()).add(c.split)
    for sid, splits in sorted(splits_by_scenario.items()):
        if len(splits) > 1:
            issues.append(Issue("S02", f"scenario {sid} appears in both splits"))
    by_text: dict[str, str] = {}
    for c in cases:
        if c.input_text in by_text:
            issues.append(Issue("S03", f"input_text identical to {by_text[c.input_text]}", c.case_id))
        by_text.setdefault(c.input_text, c.case_id)
    for sim, d, r in cross_split_similarity(cases):
        if sim < NEAR_DUPLICATE_THRESHOLD:
            break
        issues.append(Issue("S03", f"near-duplicate across splits: {d} ~ {r} (jaccard {sim:.2f})"))
    return issues


def validate_suite_dir(suite_dir: str | Path) -> list[Issue]:
    suite_dir = Path(suite_dir)
    try:
        manifest = read_manifest(suite_dir)
        cases = read_cases(suite_dir)
        reviews = read_reviews(suite_dir)
    except (OSError, ValidationError, ValueError) as exc:
        return [Issue("V01", f"cannot load suite: {exc}")]
    issues = validate_cases(cases)
    actual = hashlib.sha256((suite_dir / CASES_FILE).read_bytes()).hexdigest()
    if actual != manifest.cases_sha256:
        issues.append(Issue("S04", "cases.jsonl does not match the manifest hash (edited after generation?)"))
    if manifest.n_cases != len(cases):
        issues.append(Issue("S04", f"manifest says {manifest.n_cases} cases, file has {len(cases)}"))
    if any(c.suite_version != manifest.suite_version for c in cases):
        issues.append(Issue("S04", "case suite_version differs from manifest"))
    for c in cases:
        rec = reviews.get(c.case_id)
        if rec is not None and rec.status != "candidate" and rec.case_sha256 != case_sha256(c):
            issues.append(Issue("S05", f"{rec.status} review is stale: the case changed after review", c.case_id))
    return issues
