"""Registered scenario families. Add a family by writing a module with ``FAMILY`` and listing it here."""

from __future__ import annotations

from ..scenario import Family
from . import bad_config_rollout, memory_pressure, upstream_dependency

FAMILIES: dict[str, Family] = {
    f.name: f
    for f in (
        memory_pressure.FAMILY,
        bad_config_rollout.FAMILY,
        upstream_dependency.FAMILY,
    )
}
