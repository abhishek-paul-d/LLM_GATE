"""Dependency: workloads fail when service names cannot be resolved."""

from __future__ import annotations

from datetime import datetime

from .. import names
from ..rng import Rng
from ..scenario import Cause, Family, LogLine, Resource, Template, minutes


def _upstream_dns_failure(rng: Rng, r: Resource, t: datetime) -> Cause:
    upstream = rng.pick(names.UPSTREAMS)
    host = names.host(upstream, r.namespace)
    resolver = names.test_net_ip(rng)
    return Cause(
        category="dependency",
        logs=tuple(
            LogLine(
                minutes(t, -5, 1, rng),
                rng.pick(r.pods),
                f"ERROR dial tcp: lookup {host} on {resolver}:53: server misbehaving",
            )
            for _ in range(2)
        ),
        facts=(("server misbehaving", "DNS", "lookup", "resolve"), (upstream, host)),
        entities=(upstream, host, resolver, f"{resolver}:53"),
        check_targets=(upstream,),
    )  # fmt: skip


def _datastore_dns_timeout(rng: Rng, r: Resource, t: datetime) -> Cause:
    store, _port = rng.pick(names.DATASTORES)
    host = names.host(store, r.namespace)
    return Cause(
        category="dependency",
        logs=(
            LogLine(
                minutes(t, -5, 0, rng), rng.pick(r.pods), f"FATAL startup: could not resolve {host}: i/o timeout", fatal=True
            ),
        ),
        facts=(("could not resolve", "DNS", "i/o timeout"), (store, host)),
        entities=(store, host),
        check_targets=(store,),
    )


FAMILY = Family(
    name="dns_resolution_failure",
    category="dependency",
    templates=(
        Template("dns_resolution_failure/a", "HighErrorRate", "error_rate", _upstream_dns_failure),
        Template("dns_resolution_failure/b", "KubePodCrashLooping", "restarts_1h", _datastore_dns_timeout),
    ),
)
