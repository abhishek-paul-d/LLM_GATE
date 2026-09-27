"""Configuration: a rollout cannot start because its container image cannot be pulled."""

from __future__ import annotations

from datetime import datetime

from ..rng import Rng
from ..scenario import Cause, Family, LogLine, Resource, Template, minutes


def _unknown_image_tag(rng: Rng, r: Resource, t: datetime) -> Cause:
    tag = f"v{rng.between(1, 9)}.{rng.between(0, 30)}.{rng.between(0, 99)}"
    image = f"registry.synth.example/{r.namespace}/{r.name}:{tag}"
    revision = rng.between(4, 60)
    pod = rng.pick(r.pods)
    return Cause(
        category="configuration",
        logs=(
            LogLine(minutes(t, -8, -4, rng), "deployment-controller", f"deployment/{r.name} rolled out revision {revision} with image {image}"),
            LogLine(minutes(t, -3, 0, rng), "kubelet", f'Failed to pull image "{image}": manifest unknown; pod {pod} in ImagePullBackOff'),
        ),
        facts=((tag, image, "ImagePullBackOff", "manifest unknown"), (f"revision {revision}", "rollout", "rolled out")),
        entities=(tag, image, f"revision {revision}"),
    )  # fmt: skip


def _missing_registry_credentials(rng: Rng, r: Resource, t: datetime) -> Cause:
    image = f"registry.synth.example/{r.namespace}/{r.name}:v{rng.between(1, 9)}.{rng.between(0, 30)}.0"
    credentials = f"{r.name}-registry-creds"
    return Cause(
        category="configuration",
        logs=(
            LogLine(minutes(t, -5, -2, rng), "kubelet", f"ErrImagePull: pull access denied for {image}, repository may require authorization"),
            LogLine(
                minutes(t, -1, 1, rng),
                "kubelet",
                f"imagePullSecrets entry {credentials} not found in namespace {r.namespace}",
            ),
        ),
        facts=(("pull access denied", "ErrImagePull", "registry credentials", credentials),),
        entities=(image, credentials),
        check_targets=(credentials,),
    )  # fmt: skip


FAMILY = Family(
    name="image_pull_error",
    category="configuration",
    templates=(
        Template("image_pull_error/a", "KubePodNotReady", "unavailable_replicas", _unknown_image_tag),
        Template("image_pull_error/b", "KubePodNotReady", "unavailable_replicas", _missing_registry_credentials),
    ),
)
