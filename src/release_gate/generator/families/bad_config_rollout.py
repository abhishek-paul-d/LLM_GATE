"""Configuration: a rollout or config change leaves the service misconfigured."""

from __future__ import annotations

from datetime import datetime

from ..rng import Rng
from ..scenario import Cause, Family, LogLine, Resource, Template, minutes

ENV_VARS = ("DATABASE_URL", "CACHE_ENDPOINT", "FEATURE_FLAG_SOURCE", "SMTP_RELAY_HOST", "LEDGER_BASE_URL", "QUEUE_NAME")


def _missing_env_var(rng: Rng, r: Resource, t: datetime) -> Cause:
    var = rng.pick(ENV_VARS)
    revision = rng.between(4, 60)
    return Cause(
        category="configuration",
        logs=(
            LogLine(minutes(t, -12, -6, rng), "deployment-controller", f"deployment/{r.name} rolled out revision {revision}"),
            LogLine(minutes(t, -5, -1, rng), rng.pick(r.pods), f"FATAL config: required environment variable {var} is not set", fatal=True),
        ),
        facts=((var, "environment variable", "env var"), (f"revision {revision}", "rollout", "rolled out")),
        entities=(var, f"revision {revision}"),
    )  # fmt: skip


def _bad_config_file(rng: Rng, r: Resource, t: datetime) -> Cause:
    configmap = f"{r.name}-config"
    path = f"/etc/{r.name}/config.yaml"
    line = rng.between(3, 180)
    version = rng.between(10000, 99999)
    return Cause(
        category="configuration",
        logs=(
            LogLine(minutes(t, -14, -8, rng), "config-sync", f"configmap/{configmap} updated (resourceVersion {version})"),
            LogLine(minutes(t, -6, -1, rng), rng.pick(r.pods), f"ERROR failed to parse {path}: line {line}: mapping values are not allowed here; using defaults"),
        ),
        facts=((configmap, path, "config.yaml", "configmap", "config file"),),
        entities=(configmap, path, f"line {line}", str(version)),
        check_targets=(configmap,),
    )  # fmt: skip


FAMILY = Family(
    name="bad_config_rollout",
    category="configuration",
    templates=(
        Template("bad_config_rollout/a", "KubePodCrashLooping", "restarts_1h", _missing_env_var),
        Template("bad_config_rollout/b", "HighErrorRate", "error_rate", _bad_config_file),
    ),
)
