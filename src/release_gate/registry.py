"""Model registry: one editable YAML spec per model configuration under ``models/``.

A spec pins everything about one side of a comparison except the prompt and the suite:
which weights to load, how vLLM serves them, and the default request settings. Specs are
selected by name (``qwen3-8b-fp8`` -> ``models/qwen3-8b-fp8.yaml``) or by path. To try another
model, copy a spec, change it, and pass the new name.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_MODELS_DIR = Path("models")

_SLUG = r"^[a-z0-9][a-z0-9._-]*$"
_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class HFModel(_Strict):
    id: str = Field(pattern=r"^[\w.-]+/[\w.-]+$", description="Hugging Face repo id, e.g. Qwen/Qwen3-8B")
    # Branch, tag, or 40-char commit SHA. Release runs should pin a SHA; the run manifest
    # always records the resolved SHA either way.
    revision: str = "main"
    license: str
    gated: bool = False  # needs license acceptance and an HF token (Colab secret HF_TOKEN)


class Serving(_Strict):
    backend: Literal["vllm"] = "vllm"
    dtype: Literal["auto", "bfloat16", "float16"] = "bfloat16"
    quantization: Literal["awq", "gptq", "fp8"] | None = None
    max_model_len: int = Field(ge=512)
    # None lets the runner choose from the execution mode (sequential: whole GPU,
    # concurrent: split between both servers).
    gpu_memory_utilization: Annotated[float, Field(gt=0.0, le=1.0)] | None = None
    # Approximate weight memory, used to decide whether two specs fit on one GPU at once.
    weights_gb: float = Field(gt=0.0)
    extra_args: list[str] = []

    @model_validator(mode="after")
    def _no_managed_flags_in_extra_args(self) -> Serving:
        managed = {
            "--model", "--revision", "--dtype", "--quantization", "--max-model-len",
            "--gpu-memory-utilization", "--port", "--host", "--served-model-name", "--seed",
        }  # fmt: skip
        clash = sorted({a.split("=", 1)[0] for a in self.extra_args} & managed)
        if clash:
            raise ValueError(f"serving.extra_args must not set flags managed by the spec: {clash}")
        return self


JSONScalar = bool | int | float | str


class RequestDefaults(_Strict):
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0)
    max_tokens: int = Field(default=512, ge=1)
    seed: int | None = 1234
    # "none": the model must produce valid JSON on its own, so schema validity is measured.
    # "json_schema": vLLM constrains decoding to the triage schema. Changing this is a
    # configuration change under test (plan.md §14).
    structured_output: Literal["none", "json_schema"] = "none"
    chat_template_kwargs: dict[str, JSONScalar] = {}


class ModelSpec(_Strict):
    spec_version: Literal[1]
    name: str = Field(pattern=_SLUG)
    description: str = ""
    model: HFModel
    serving: Serving
    request: RequestDefaults = RequestDefaults()

    @model_validator(mode="after")
    def _reasoning_mode_explicit(self) -> ModelSpec:
        # Qwen3 chat templates think by default: <think>...</think> precedes the answer,
        # breaking JSON output and inflating latency. Require an explicit choice.
        if self.model.id.startswith("Qwen/Qwen3") and "enable_thinking" not in self.request.chat_template_kwargs:
            raise ValueError(
                f"{self.model.id} thinks by default; set request.chat_template_kwargs.enable_thinking "
                "explicitly (false for the triage workload)"
            )
        return self

    @property
    def revision_pinned(self) -> bool:
        return bool(_COMMIT_SHA.match(self.model.revision))

    def vllm_serve_args(self, port: int, gpu_memory_utilization: float | None = None) -> list[str]:
        """Arguments for ``vllm serve`` that reproduce this spec. Deterministic order."""
        # Validate overrides here: vLLM would only reject them on the GPU host, inside a paid session.
        if not 1 <= port <= 65535:
            raise ValueError(f"port must be 1-65535, got {port}")
        if gpu_memory_utilization is not None and not 0.0 < gpu_memory_utilization <= 1.0:
            raise ValueError(f"gpu_memory_utilization must be in (0, 1], got {gpu_memory_utilization}")
        util = gpu_memory_utilization if gpu_memory_utilization is not None else self.serving.gpu_memory_utilization
        s = self.serving
        args = [
            "vllm", "serve", self.model.id,
            "--revision", self.model.revision,
            "--served-model-name", self.name,
            "--dtype", s.dtype,
            "--max-model-len", str(s.max_model_len),
            "--host", "127.0.0.1",
            "--port", str(port),
        ]  # fmt: skip
        if s.quantization is not None:
            args += ["--quantization", s.quantization]
        if util is not None:
            args += ["--gpu-memory-utilization", f"{util:g}"]
        if self.request.seed is not None:
            args += ["--seed", str(self.request.seed)]
        return args + list(s.extra_args)

    def request_params(self) -> dict[str, Any]:
        """Default OpenAI-compatible chat.completions parameters (``extra_body`` for vLLM-only fields)."""
        r = self.request
        params: dict[str, Any] = {
            "model": self.name,
            "temperature": r.temperature,
            "top_p": r.top_p,
            "max_tokens": r.max_tokens,
        }
        if r.seed is not None:
            params["seed"] = r.seed
        if r.chat_template_kwargs:
            params["extra_body"] = {"chat_template_kwargs": dict(r.chat_template_kwargs)}
        return params


# --------------------------------------------------------------------------- loading


def _resolve(ref: str | Path, models_dir: Path) -> Path:
    ref_str = str(ref)
    if ref_str.endswith((".yaml", ".yml")) or "/" in ref_str or "\\" in ref_str:
        return Path(ref_str)
    return models_dir / f"{ref_str}.yaml"


def list_model_specs(models_dir: str | Path = DEFAULT_MODELS_DIR) -> list[str]:
    """Names of the specs in ``models_dir``, sorted. A missing directory is an error, not an empty registry."""
    models_dir = Path(models_dir)
    if not models_dir.is_dir():
        raise NotADirectoryError(f"models directory {str(models_dir)!r} does not exist or is not a directory")
    return sorted(p.stem for p in models_dir.glob("*.yaml"))


def load_model_spec(ref: str | Path, models_dir: str | Path = DEFAULT_MODELS_DIR) -> ModelSpec:
    """Load a spec by name (``qwen3-8b-fp8``) or path (``models/qwen3-8b-fp8.yaml``).

    The spec's ``name`` must equal its file stem so a name always means one file. Every
    parse or validation error names the offending file.
    """
    models_dir = Path(models_dir)
    path = _resolve(ref, models_dir)
    if not path.is_file():
        available = ", ".join(list_model_specs(models_dir)) if models_dir.is_dir() else f"{models_dir} missing"
        raise FileNotFoundError(f"model spec {ref!r} not found at {path} (available: {available or 'none'})")
    try:
        with open(path, encoding="utf-8") as f:
            spec = ModelSpec.model_validate(yaml.safe_load(f))
    except (yaml.YAMLError, ValidationError) as exc:
        raise ValueError(f"{path}: invalid model spec: {exc}") from exc
    if spec.name != path.stem:
        raise ValueError(f"{path}: spec name {spec.name!r} must match the file name {path.stem!r}")
    return spec


def fits_concurrently(specs: list[ModelSpec], gpu_memory_gb: float, headroom: float = 0.35) -> bool:
    """True when all specs' weights fit on one GPU with ``headroom`` of it left for KV cache and overhead.

    Used to pick the execution mode: concurrent (both servers up, requests interleaved) when it
    fits, otherwise sequential (one server at a time on the whole GPU, same session).
    """
    return sum(s.serving.weights_gb for s in specs) <= gpu_memory_gb * (1.0 - headroom)
