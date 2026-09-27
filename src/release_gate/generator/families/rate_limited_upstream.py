"""Dependency: an upstream service rejects calls after rate or quota limits are reached."""

from __future__ import annotations

from datetime import datetime

from .. import names
from ..rng import Rng
from ..scenario import Cause, Family, LogLine, Metric, Resource, Template, minutes


def _upstream_rate_limit(rng: Rng, r: Resource, t: datetime) -> Cause:
    upstream = rng.pick(names.UPSTREAMS)
    host = names.host(upstream, r.namespace)
    retry = rng.between(5, 120)
    rate = Metric("upstream_429_rate", round(rng.uniform(10, 80), 1), "%")
    return Cause(
        category="dependency",
        metrics=(rate,),
        logs=tuple(
            LogLine(
                minutes(t, -5, 1, rng),
                rng.pick(r.pods),
                f"WARN https://{host}/v1/score responded 429 Too Many Requests; retry-after={retry}s",
            )
            for _ in range(2)
        ),
        facts=(("429", "Too Many Requests", "rate limit", "rate-limited"), (upstream, host)),
        entities=(upstream, host, f"retry-after={retry}s"),
        check_targets=(upstream,),
    )  # fmt: skip


def _upstream_quota_exhausted(rng: Rng, r: Resource, t: datetime) -> Cause:
    upstream = rng.pick(names.UPSTREAMS)
    project = f"synth-{rng.hex(6)}"
    return Cause(
        category="dependency",
        logs=(
            LogLine(
                minutes(t, -5, 0, rng),
                rng.pick(r.pods),
                f"ERROR {upstream} quota exceeded for project {project}: daily request quota used up; requests rejected until reset",
            ),
        ),
        facts=(("quota exceeded", "quota"), (upstream,)),
        entities=(upstream, project),
        check_targets=(upstream,),
    )


FAMILY = Family(
    name="rate_limited_upstream",
    category="dependency",
    templates=(
        Template("rate_limited_upstream/a", "HighErrorRate", "error_rate", _upstream_rate_limit),
        Template("rate_limited_upstream/b", "UpstreamQuotaExhausted", "error_rate", _upstream_quota_exhausted),
    ),
)
