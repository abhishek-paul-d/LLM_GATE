"""Registered scenario families. Add a family by writing a module with ``FAMILY`` and listing it here."""

from __future__ import annotations

from ..scenario import Family
from . import (
    bad_config_rollout,
    cpu_throttling,
    disk_pressure,
    dns_resolution_failure,
    image_pull_error,
    memory_pressure,
    probe_misconfig,
    rate_limited_upstream,
    upstream_dependency,
)

FAMILIES: dict[str, Family] = {
    f.name: f
    for f in (
        memory_pressure.FAMILY,
        bad_config_rollout.FAMILY,
        upstream_dependency.FAMILY,
        cpu_throttling.FAMILY,
        disk_pressure.FAMILY,
        image_pull_error.FAMILY,
        probe_misconfig.FAMILY,
        dns_resolution_failure.FAMILY,
        rate_limited_upstream.FAMILY,
    )
}
