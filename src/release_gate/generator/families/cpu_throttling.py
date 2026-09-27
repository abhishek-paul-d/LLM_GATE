"""Capacity: CPU throttling leaves work slow or an autoscaler maxed out."""

from __future__ import annotations

from datetime import datetime

from ..rng import Rng
from ..scenario import Cause, Family, LogLine, Metric, Resource, Template, minutes


def _cpu_usage(rng: Rng) -> tuple[int, Metric]:
    limit = rng.pick((250, 500, 1000, 2000))
    usage = Metric("cpu_usage", float(round(limit * rng.uniform(0.95, 1.0))), "millicores", limit=limit)
    return limit, usage


def _throttled_container(rng: Rng, r: Resource, t: datetime) -> Cause:
    _, usage = _cpu_usage(rng)
    throttled = Metric("cpu_throttled_periods", round(rng.uniform(40, 90), 1), "%")
    lag = rng.between(300, 4000)
    return Cause(
        category="capacity",
        metrics=(usage, throttled),
        logs=(
            LogLine(
                minutes(t, -5, 0, rng),
                rng.pick(r.pods),
                f"WARN request handling slow: event loop lag {lag} ms while CPU-throttled at the container limit",
            ),
        ),
        facts=(("throttled", "throttling", "CPU limit"),),
        entities=(f"{lag} ms",),
    )  # fmt: skip


def _autoscaler_maxed(rng: Rng, r: Resource, t: datetime) -> Cause:
    _, usage = _cpu_usage(rng)
    desired = r.replicas + rng.between(2, 8)
    utilization = rng.between(120, 260)
    return Cause(
        category="capacity",
        metrics=(usage,),
        logs=(
            LogLine(
                minutes(t, -5, 0, rng),
                "horizontal-pod-autoscaler",
                f"deployment/{r.name}: desired replicas {desired} exceeds max replicas {r.replicas}; cpu utilization {utilization}% of request",
            ),
        ),
        facts=(("max replicas", "autoscaler", "HPA", "CPU"),),
        entities=(f"desired replicas {desired}", f"max replicas {r.replicas}", f"{utilization}%"),
    )  # fmt: skip


FAMILY = Family(
    name="cpu_throttling",
    category="capacity",
    templates=(
        Template("cpu_throttling/a", "CPUThrottlingHigh", "error_rate", _throttled_container),
        Template("cpu_throttling/b", "HPAMaxedOut", "error_rate", _autoscaler_maxed),
    ),
)
