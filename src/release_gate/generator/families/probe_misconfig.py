"""Configuration: a health probe disagrees with the application startup or listener."""

from __future__ import annotations

from datetime import datetime

from .. import names
from ..rng import Rng
from ..scenario import Cause, Family, LogLine, Resource, Template, minutes


def _readiness_port_mismatch(rng: Rng, r: Resource, t: datetime) -> Cause:
    port = rng.pick((8080, 8000, 3000))
    wrong = rng.pick((9090, 8081, 5000))
    ip = names.test_net_ip(rng)
    revision = rng.between(4, 60)
    pod = rng.pick(r.pods)
    return Cause(
        category="configuration",
        logs=(
            LogLine(
                minutes(t, -8, -5, rng),
                "deployment-controller",
                f"deployment/{r.name} rolled out revision {revision}: readinessProbe.httpGet.port changed to {wrong}",
            ),
            LogLine(minutes(t, -4, -2, rng), pod, f"server listening on :{port}"),
            LogLine(
                minutes(t, -1, 1, rng),
                "kubelet",
                f'Readiness probe failed: Get "http://{ip}:{wrong}/ready": dial tcp {ip}:{wrong}: connect: connection refused',
            ),
        ),
        facts=(
            ("readinessProbe", "readiness probe", "probe port"),
            (f"revision {revision}", "rollout", "rolled out"),
        ),
        entities=(ip, f"{ip}:{wrong}", f":{port}", f"revision {revision}"),
    )  # fmt: skip


def _liveness_startup_too_short(rng: Rng, r: Resource, t: datetime) -> Cause:
    delay = rng.pick((3, 5, 10))
    startup = rng.between(25, 90)
    pod = rng.pick(r.pods)
    return Cause(
        category="configuration",
        logs=(
            LogLine(
                minutes(t, -8, -5, rng),
                "deployment-controller",
                f"deployment/{r.name} spec updated: livenessProbe.initialDelaySeconds={delay}",
            ),
            LogLine(minutes(t, -4, -2, rng), pod, f"startup completed in {startup}s"),
            LogLine(
                minutes(t, -1, 1, rng),
                "kubelet",
                f"Liveness probe failed for pod {pod}: HTTP probe failed with statuscode: 503; container will be restarted",
            ),
        ),
        facts=(("initialDelaySeconds", "liveness probe", "livenessProbe"),),
        entities=(f"initialDelaySeconds={delay}", f"{startup}s"),
    )  # fmt: skip


FAMILY = Family(
    name="probe_misconfig",
    category="configuration",
    templates=(
        Template("probe_misconfig/a", "KubePodNotReady", "unavailable_replicas", _readiness_port_mismatch),
        Template("probe_misconfig/b", "KubeContainerRestarting", "restarts_1h", _liveness_startup_too_short),
    ),
)
