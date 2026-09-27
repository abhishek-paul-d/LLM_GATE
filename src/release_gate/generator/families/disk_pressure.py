"""Capacity: persistent volumes fill or nodes run low on ephemeral storage."""

from __future__ import annotations

from datetime import datetime

from ..rng import Rng
from ..scenario import Cause, Family, LogLine, Metric, Resource, Template, minutes


def _persistent_volume_full(rng: Rng, r: Resource, t: datetime) -> Cause:
    pvc = f"data-{r.name}-0"
    capacity = rng.pick((10, 20, 50))
    used = Metric("pvc_used", round(capacity * rng.uniform(0.97, 1.0), 2), "GiB", limit=capacity)
    return Cause(
        category="capacity",
        metrics=(used,),
        logs=(
            LogLine(
                minutes(t, -5, 0, rng),
                "kubelet",
                f"ERROR write failed: no space left on device (path /var/lib/{r.name}/data, pvc {pvc})",
            ),
        ),
        facts=(("no space left", "disk full", "volume full", "PVC"),),
        entities=(pvc, f"/var/lib/{r.name}/data"),
        check_targets=(pvc,),
    )  # fmt: skip


def _ephemeral_storage_low(rng: Rng, r: Resource, t: datetime) -> Cause:
    node = f"node-{rng.hex(6)}"
    limit = rng.pick((2048, 4096, 8192))
    used = Metric("ephemeral_storage_used", float(round(limit * rng.uniform(0.96, 1.0))), "MiB", limit=limit)
    pod = rng.pick(r.pods)
    return Cause(
        category="capacity",
        metrics=(used,),
        logs=(
            LogLine(
                minutes(t, -5, 0, rng),
                "kubelet",
                f"Evicted pod {pod}: The node {node} was low on resource: ephemeral-storage",
            ),
        ),
        facts=(("ephemeral-storage", "evicted", "disk pressure", "low on resource"),),
        entities=(node,),
    )  # fmt: skip


FAMILY = Family(
    name="disk_pressure",
    category="capacity",
    templates=(
        Template("disk_pressure/a", "KubePersistentVolumeFillingUp", "error_rate", _persistent_volume_full),
        Template("disk_pressure/b", "KubePodEvicted", "unavailable_replicas", _ephemeral_storage_low),
    ),
)
