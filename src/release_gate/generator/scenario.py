"""Scenario building blocks: a draft = resource + symptom + zero or more causes, rendered to alert text.

Severity comes from the symptom (``severity.py``). Category comes from the causes: one cause
gives its family's category, zero causes (missing evidence) or causes from two categories
(conflicting) give ``unknown``. Variants in ``variants.py`` only move these pieces around, so
label logic lives in one place: ``Draft.to_case``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from ..schemas.triage import Category
from . import names
from .rng import Rng
from .schema import (
    Behavior,
    Difficulty,
    Expected,
    Injection,
    MetricRecord,
    SuiteCase,
    SymptomRecord,
    Variant,
)
from .severity import ERROR_RATE_BANDS, RESTART_BANDS, SEVERITIES, Severity, SymptomKind, severity_for

EPOCH = datetime(2026, 3, 1, tzinfo=UTC)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- pieces


@dataclass(frozen=True)
class Metric:
    name: str
    value: float
    unit: str
    limit: float | None = None

    @property
    def pct(self) -> int | None:
        return None if not self.limit or self.unit in ("count", "ms") else round(self.value / self.limit * 100)

    def display(self) -> str:
        v, lim = self.value, self.limit
        if self.unit == "%":
            return f"{v:.1f}%"
        if self.unit in ("GiB", "MiB"):
            s = f"{v:.2f} {self.unit}" if self.unit == "GiB" else f"{v:.0f} MiB"
            return s if lim is None else f"{s} (limit {lim:g} {self.unit}, {self.pct}%)"
        if self.unit == "millicores":
            return f"{v:.0f}m" if lim is None else f"{v:.0f}m (limit {lim:.0f}m, {self.pct}%)"
        if self.unit == "ms":
            return f"{v:.0f} ms" if lim is None else f"{v:.0f} ms (timeout {lim:.0f} ms)"
        if self.unit == "count":
            return f"{v:.0f}" if lim is None else f"{v:.0f} of {lim:.0f}"
        raise ValueError(f"unknown unit {self.unit!r}")

    def entity_values(self) -> list[str]:
        """Concrete values a model could restate; each is a substring of display()."""
        v, lim = self.value, self.limit
        if self.unit == "%":
            return [f"{v:.1f}%"]
        if self.unit == "GiB":
            return [f"{v:.2f} GiB"] + ([] if lim is None else [f"{lim:g} GiB", f"{self.pct}%"])
        if self.unit == "MiB":
            return [f"{v:.0f} MiB"] + ([] if lim is None else [f"{lim:g} MiB", f"{self.pct}%"])
        if self.unit == "millicores":
            return [f"{v:.0f}m"] + ([] if lim is None else [f"{lim:.0f}m", f"{self.pct}%"])
        if self.unit == "ms":
            return [f"{v:.0f} ms"] + ([] if lim is None else [f"{lim:.0f} ms"])
        return [self.display()]

    def record(self) -> MetricRecord:
        return MetricRecord(name=self.name, value=self.value, unit=self.unit, limit=self.limit, display=self.display())


@dataclass(frozen=True)
class LogLine:
    at: datetime
    source: str
    message: str
    # The container exits at startup (e.g. a FATAL config error), so this pod never serves
    # traffic: ``Draft.logs`` moves the pod's other lines to a healthy pod.
    fatal: bool = False


@dataclass(frozen=True)
class Resource:
    cluster: str
    namespace: str
    kind: str
    name: str
    replicas: int
    pods: tuple[str, ...]
    team: str
    tier: str

    def entities(self) -> list[str]:
        return [self.cluster, self.namespace, self.name, *self.pods]


@dataclass(frozen=True)
class Cause:
    """Evidence that reveals a root cause. Everything in it must be stated in the rendered text."""

    category: Category
    metrics: tuple[Metric, ...] = ()
    logs: tuple[LogLine, ...] = ()
    facts: tuple[tuple[str, ...], ...] = ()  # fact groups this evidence supports (first = the key fact)
    entities: tuple[str, ...] = ()  # extra concrete values introduced (hosts, variables, files)
    check_targets: tuple[str, ...] = ()  # extra resources a next_check may reference


CauseBuilder = Callable[[Rng, Resource, datetime], Cause]


@dataclass(frozen=True)
class Template:
    scenario_id: str  # "<family>/<letter>"
    alert_name: str
    symptom_kind: SymptomKind
    build_cause: CauseBuilder


@dataclass(frozen=True)
class Family:
    name: str
    category: Category
    templates: tuple[Template, ...]


# --------------------------------------------------------------------------- sampling helpers


def sample_resource(rng: Rng) -> Resource:
    name = rng.pick(names.SERVICES)
    replicas = rng.between(3, 6)
    pods = tuple(names.pod_name(rng, name) for _ in range(min(replicas, 3)))
    return Resource(
        cluster=rng.pick(names.CLUSTERS),
        namespace=rng.pick(names.NAMESPACES),
        kind="deployment",
        name=name,
        replicas=replicas,
        pods=pods,
        team=rng.pick(names.TEAMS),
        tier=rng.pick(names.TIERS),
    )


def sample_symptom(rng: Rng, kind: SymptomKind, resource: Resource) -> Metric:
    """Pick a severity uniformly, then a symptom value inside that band (balanced labels)."""
    target: Severity = rng.pick(SEVERITIES)
    if kind == "error_rate":
        lo, hi = ERROR_RATE_BANDS[target]
        return Metric("http_5xx_rate", round(rng.uniform(lo, hi), 1), "%")
    if kind == "restarts_1h":
        lo, hi = RESTART_BANDS[target]
        return Metric("container_restarts_1h", rng.between(lo, hi), "count")
    if kind == "unavailable_replicas":
        d = resource.replicas
        half = -(-d // 2)  # ceil(d / 2)
        lo, hi = {"low": (1, half - 1), "medium": (half, d - 1), "high": (d, d)}[target]
        return Metric("unavailable_replicas", rng.between(lo, hi), "count", limit=d)
    raise ValueError(kind)


def sample_start(rng: Rng) -> datetime:
    return EPOCH + timedelta(days=rng.between(0, 27), minutes=rng.between(0, 24 * 60 - 1))


def minutes(t: datetime, lo: int, hi: int, rng: Rng) -> datetime:
    return t + timedelta(minutes=rng.between(lo, hi), seconds=rng.between(0, 59))


# KubePodCrashLooping fires after 15 minutes in CrashLoopBackOff, which means many restarts.
# With a single restart in the hour the realistic alert is the generic restart alert.
CRASH_LOOP_ALERT, SINGLE_RESTART_ALERT, CRASH_LOOP_MIN_RESTARTS = "KubePodCrashLooping", "KubeContainerRestarting", 2


# What the output must say about the symptom when there is no cause to name. Concept phrases,
# not bare numbers: a lone "3" would be satisfied by any 3 in a summary.
SYMPTOM_FACTS: dict[str, Callable[[Metric], tuple[str, ...]]] = {
    "error_rate": lambda m: (m.display(), "5xx", "500", "error rate", "errors"),
    "restarts_1h": lambda m: ("restart", "crash", "back-off", "backoff"),
    "unavailable_replicas": lambda m: (m.display(), "unavailable", "not available", "not ready"),
}


# Alert names that describe only the symptom. A missing-evidence case must use one of these:
# a cause-specific name such as KubePersistentVolumeFillingUp is itself evidence of the cause,
# and would make the expected "unknown" label unfair to a model that reasons from it.
SYMPTOM_ALERTS: dict[str, tuple[str, ...]] = {
    "error_rate": ("HighErrorRate",),
    "restarts_1h": ("KubeContainerRestarting", "KubePodCrashLooping"),
    "unavailable_replicas": ("KubeDeploymentReplicasUnavailable", "KubePodNotReady"),
}


# Symptom lines that describe the impact without pointing at any cause category. Once the
# cause is removed these lines are all that distinguishes a case, so phrasings are
# split-specific: otherwise a dev and a release template sharing an alert name would render
# near-identical missing-evidence cases (caught by the S03 near-duplicate check).
GENERIC_SYMPTOM_PHRASINGS: dict[str, dict[str, tuple[str, ...]]] = {
    "dev": {
        "error_rate": ("request failed status=500 path={path}", "handler error: returning 500 for {path}"),
        "restarts_1h": ("Back-off restarting failed container {name} in pod {pod}",),
        "unavailable_replicas": ("deployment/{name}: {display} replicas unavailable; waiting for pods to become ready",),
    },
    "release": {
        "error_rate": ("upstream_status=500 route={path} outcome=error", "responded 500 Internal Server Error to {path}"),
        "restarts_1h": ("container {name} in {pod} exited; restarting with back-off",),
        "unavailable_replicas": ("rollout status for {name}: {display} replicas not available",),
    },
}


def generic_symptom_logs(rng: Rng, kind: SymptomKind, r: Resource, symptom: Metric, t: datetime, split: str) -> list[LogLine]:
    phrasings = GENERIC_SYMPTOM_PHRASINGS[split][kind]
    pod = rng.pick(r.pods)
    source = {"error_rate": pod, "restarts_1h": "kubelet", "unavailable_replicas": "deployment-controller"}[kind]
    path = rng.pick(("/api/v1/orders", "/api/v1/items", "/api/v1/sessions", "/api/v1/reports"))
    count = 2 if kind == "error_rate" else 1
    return [
        LogLine(
            minutes(t, -4, 2, rng),
            source,
            rng.pick(phrasings).format(path=path, name=r.name, pod=pod, display=symptom.display()),
        )
        for _ in range(count)
    ]


# --------------------------------------------------------------------------- draft


@dataclass
class Draft:
    family: Family
    template: Template
    split: str
    variant: Variant
    resource: Resource
    started_at: datetime
    symptom: Metric
    causes: list[Cause]
    context_logs: list[LogLine]
    status: str = "firing"
    resolved_at: datetime | None = None
    extra_metrics: list[Metric] = field(default_factory=list)
    extra_logs: list[LogLine] = field(default_factory=list)
    extra_entities: list[str] = field(default_factory=list)
    extra_facts: list[tuple[str, ...]] = field(default_factory=list)
    labels_text: str | None = None  # override for malformed labels
    alert_override: str | None = None  # symptom-only alert name when the cause is removed
    tags: list[str] = field(default_factory=list)
    difficulty: Difficulty = "easy"
    injection: Injection | None = None

    @property
    def alert_name(self) -> str:
        return self.alert_override or self.template.alert_name

    # ------------------------------------------------------------------ labels

    def expected_category(self) -> Category:
        categories = {c.category for c in self.causes}
        return categories.pop() if len(categories) == 1 else "unknown"

    def acceptable_categories(self) -> list[Category]:
        causes = list(dict.fromkeys(c.category for c in self.causes))
        return ["unknown", *causes] if len(causes) > 1 else [self.expected_category()]

    def behavior(self) -> Behavior:
        return "answer" if self.expected_category() != "unknown" else "ask_for_signal"

    def expected_severity(self) -> Severity:
        return severity_for(self.template.symptom_kind, self.symptom.value, self.symptom.limit, self.status == "resolved")

    def required_facts(self) -> list[list[str]]:
        facts: list[tuple[str, ...]] = [(self.resource.name,)]
        if not self.causes:
            facts.append(SYMPTOM_FACTS[self.template.symptom_kind](self.symptom))
        facts += [c.facts[0] for c in self.causes if c.facts]
        if len(self.causes) == 1:
            facts += list(self.causes[0].facts[1:])
        facts += self.extra_facts
        return [list(f) for f in facts]

    # ------------------------------------------------------------------ rendering

    def metrics(self) -> list[Metric]:
        out = [self.symptom, *self.extra_metrics]
        for c in self.causes:
            out += c.metrics
        return out

    def logs(self) -> list[LogLine]:
        lines = [*self.context_logs, *self.extra_logs]
        for c in self.causes:
            lines += c.logs
        # A pod whose container dies at startup never serves traffic, so any other line
        # attributed to it moves to the first healthy pod (no RNG draw: other cases are unchanged).
        crashed = {line.source for line in lines if line.fatal}
        healthy = [p for p in self.resource.pods if p not in crashed]
        if crashed and healthy:
            lines = [line if line.fatal or line.source not in crashed else replace(line, source=healthy[0]) for line in lines]
        return sorted(lines, key=lambda line: line.at)  # stable: ties keep insertion order

    def render(self) -> str:
        r = self.resource
        labels = self.labels_text or json.dumps({"team": r.team, "tier": r.tier, "alertname": self.alert_name})
        out = [
            f"[ALERT] {self.alert_name}",
            f"status: {self.status}",
            f"cluster: {r.cluster}",
            f"namespace: {r.namespace}",
            f"resource: {r.kind}/{r.name}",
            f"pods: {', '.join(r.pods)}" + (f" (+{r.replicas - len(r.pods)} more)" if r.replicas > len(r.pods) else ""),
            f"started_at: {iso(self.started_at)}",
        ]
        if self.resolved_at is not None:
            out.append(f"resolved_at: {iso(self.resolved_at)}")
        out.append(f"labels: {labels}")
        out.append("metrics:")
        out += [f"  {m.name}: {m.display()}" for m in self.metrics()]
        out.append("logs:")
        out += [f"  {iso(line.at)} {line.source} {line.message}" for line in self.logs()]
        return "\n".join(out) + "\n"

    def entities(self) -> list[str]:
        values = [*self.resource.entities()]
        for m in self.metrics():
            values += m.entity_values()
        for c in self.causes:
            values += c.entities
        values += self.extra_entities
        return list(dict.fromkeys(values))  # dedupe, keep order

    def next_check_targets(self) -> list[str]:
        targets = [self.resource.name]
        for c in self.causes:
            targets += c.check_targets
        return list(dict.fromkeys(targets))

    def to_case(self, case_id: str, suite_version: str, seed: int) -> SuiteCase:
        return SuiteCase(
            case_id=case_id,
            suite_version=suite_version,
            scenario_id=self.template.scenario_id,
            family=self.family.name,
            variant=self.variant,
            split=self.split,
            seed=seed,
            difficulty=self.difficulty,
            tags=list(dict.fromkeys(self.tags)),
            started_at=iso(self.started_at),
            resolved_at=None if self.resolved_at is None else iso(self.resolved_at),
            input_text=self.render(),
            symptom=SymptomRecord(
                kind=self.template.symptom_kind, metric=self.symptom.record(), recovered=self.status == "resolved"
            ),
            metrics=[m.record() for m in self.metrics()],
            entities=self.entities(),
            injection=self.injection,
            expected=Expected(
                category=self.expected_category(),
                acceptable_categories=self.acceptable_categories(),
                severity=self.expected_severity(),
                behavior=self.behavior(),
                required_facts=self.required_facts(),
                next_check_targets=self.next_check_targets(),
            ),
        )


def base_draft(family: Family, template: Template, split: str, rng: Rng) -> Draft:
    """The clear case: one cause, symptom, and a benign context line."""
    r = sample_resource(rng)
    t = sample_start(rng)
    symptom = sample_symptom(rng, template.symptom_kind, r)
    cause = template.build_cause(rng, r, t)
    if cause.category != family.category:
        raise ValueError(f"{template.scenario_id} built a {cause.category} cause for a {family.category} family")
    context = [LogLine(minutes(t, -25, -15, rng), rng.pick(r.pods), "GET /healthz 200")]
    single_restart = template.alert_name == CRASH_LOOP_ALERT and symptom.value < CRASH_LOOP_MIN_RESTARTS
    return Draft(
        family=family,
        template=template,
        split=split,
        variant="clear",
        resource=r,
        started_at=t,
        symptom=symptom,
        causes=[cause],
        context_logs=context,
        alert_override=SINGLE_RESTART_ALERT if single_restart else None,
        tags=["clear"],
    )
