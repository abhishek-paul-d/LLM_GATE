"""Dependency: a service fails because something it calls is slow or unreachable."""

from __future__ import annotations

from datetime import datetime

from .. import names
from ..rng import Rng
from ..scenario import Cause, Family, LogLine, Metric, Resource, Template, minutes


def _upstream_timeout(rng: Rng, r: Resource, t: datetime) -> Cause:
    upstream = rng.pick(names.UPSTREAMS)
    host = names.host(upstream, r.namespace)
    timeout = rng.pick((2000, 3000, 5000))
    p99 = Metric("upstream_p99_latency_ms", float(rng.between(timeout, timeout * 6)), "ms", limit=timeout)
    return Cause(
        category="dependency",
        metrics=(p99,),
        logs=tuple(
            LogLine(minutes(t, -5, 1, rng), rng.pick(r.pods), f"ERROR call to https://{host}/v2/check timed out after {timeout} ms (504 Gateway Timeout)")
            for _ in range(2)
        ),
        facts=(("timed out", "timeout", "504"), (upstream, host)),
        entities=(upstream, host),
        check_targets=(upstream,),
    )  # fmt: skip


def _datastore_refused(rng: Rng, r: Resource, t: datetime) -> Cause:
    store, port = rng.pick(names.DATASTORES)
    ip = names.test_net_ip(rng)
    endpoint = f"{ip}:{port}"
    return Cause(
        category="dependency",
        logs=(
            LogLine(minutes(t, -6, -1, rng), rng.pick(r.pods), f"ERROR {store} client: dial tcp {endpoint}: connect: connection refused"),
            LogLine(minutes(t, -4, 0, rng), rng.pick(r.pods), f"readiness check failed: dependency {store} unreachable at {endpoint}"),
        ),
        facts=(("connection refused", "unreachable"), (store, endpoint)),
        entities=(store, ip, endpoint),
        check_targets=(store,),
    )  # fmt: skip


FAMILY = Family(
    name="upstream_dependency",
    category="dependency",
    templates=(
        Template("upstream_dependency/a", "HighErrorRate", "error_rate", _upstream_timeout),
        Template("upstream_dependency/b", "KubeDeploymentReplicasUnavailable", "unavailable_replicas", _datastore_refused),
    ),
)
