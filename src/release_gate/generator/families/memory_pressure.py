"""Capacity: containers killed for exceeding their memory limit."""

from __future__ import annotations

from datetime import datetime

from ..rng import Rng
from ..scenario import Cause, Family, LogLine, Metric, Resource, Template, minutes


def _oomkilled(rng: Rng, r: Resource, t: datetime) -> Cause:
    limit = rng.pick((1, 2, 4))
    used = Metric("memory_working_set", round(limit * rng.uniform(0.93, 1.0), 2), "GiB", limit=limit)
    pod = rng.pick(r.pods)
    return Cause(
        category="capacity",
        metrics=(used,),
        logs=(
            LogLine(minutes(t, -6, -1, rng), "kubelet", f"pod {pod}: container {r.name} Last State: Terminated, Reason: OOMKilled, exit code 137"),
        ),
        facts=(("OOMKilled", "OOM", "out of memory", "memory limit"),),
    )  # fmt: skip


def _cgroup_oom(rng: Rng, r: Resource, t: datetime) -> Cause:
    limit = rng.pick((512, 768, 1024, 1536))
    used = Metric("container_memory_usage", float(round(limit * rng.uniform(0.95, 1.0))), "MiB", limit=limit)
    pid = rng.between(1200, 48000)
    node = f"node-{rng.hex(6)}"
    return Cause(
        category="capacity",
        metrics=(used,),
        logs=(
            LogLine(minutes(t, -8, -2, rng), f"kernel@{node}", f"Memory cgroup out of memory: Killed process {pid} ({r.name}) in pod {rng.pick(r.pods)}"),
        ),
        facts=(("out of memory", "OOM", "memory cgroup", "memory limit"),),
        entities=(node, str(pid)),
    )  # fmt: skip


FAMILY = Family(
    name="memory_pressure",
    category="capacity",
    templates=(
        Template("memory_pressure/a", "KubeContainerRestarting", "restarts_1h", _oomkilled),
        Template("memory_pressure/b", "KubeDeploymentReplicasUnavailable", "unavailable_replicas", _cgroup_oom),
    ),
)
