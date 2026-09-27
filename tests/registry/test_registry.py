"""Model registry: the shipped specs load, selection by name/path works, and footguns are refused."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from release_gate.registry import ModelSpec, fits_concurrently, list_model_specs, load_model_spec

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "models"


def _spec_doc(name: str = "qwen3-8b-fp8") -> dict[str, Any]:
    return yaml.safe_load((MODELS / f"{name}.yaml").read_text(encoding="utf-8"))


def _write(tmp_path: Path, doc: dict[str, Any], stem: str | None = None) -> Path:
    path = tmp_path / f"{stem or doc['name']}.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- shipped specs


def test_shipped_specs_listed():
    assert list_model_specs(MODELS) == ["llama-3.1-8b-instruct-fp8", "qwen3-8b-fp8"]


@pytest.mark.parametrize("name", ["llama-3.1-8b-instruct-fp8", "qwen3-8b-fp8"])
def test_shipped_specs_load_by_name_and_path(name):
    by_name = load_model_spec(name, MODELS)
    by_path = load_model_spec(MODELS / f"{name}.yaml")
    assert by_name == by_path
    assert by_name.name == name


def test_llama_spec():
    s = load_model_spec("llama-3.1-8b-instruct-fp8", MODELS)
    assert (s.model.id, s.model.gated, s.request.temperature) == ("meta-llama/Llama-3.1-8B-Instruct", True, 0.0)


def test_qwen_spec_disables_thinking():
    s = load_model_spec("qwen3-8b-fp8", MODELS)
    assert s.model.id == "Qwen/Qwen3-8B"
    assert s.request.chat_template_kwargs == {"enable_thinking": False}
    assert s.request_params()["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}


@pytest.mark.parametrize("name", ["llama-3.1-8b-instruct-fp8", "qwen3-8b-fp8"])
def test_shipped_specs_use_fp8_weights_with_bf16_activations(name):
    s = load_model_spec(name, MODELS)
    assert (s.serving.quantization, s.serving.dtype) == ("fp8", "bfloat16")
    assert s.serving.weights_gb < 10


def test_fp8_pair_fits_concurrently_on_40gb():
    pair = [load_model_spec(n, MODELS) for n in list_model_specs(MODELS)]
    assert fits_concurrently(pair, gpu_memory_gb=40)


def test_bf16_pair_would_need_sequential_on_40gb():
    pair = []
    for name in list_model_specs(MODELS):
        doc = _spec_doc(name)
        doc["serving"].update(quantization=None, weights_gb=16.4)
        pair.append(ModelSpec.model_validate(doc))
    assert not fits_concurrently(pair, gpu_memory_gb=40)
    assert fits_concurrently(pair, gpu_memory_gb=80)


# --------------------------------------------------------------------------- selection


def test_unknown_name_lists_available():
    with pytest.raises(FileNotFoundError, match="qwen3-8b-fp8"):
        load_model_spec("mistral-7b", MODELS)


def test_name_must_match_file(tmp_path):
    path = _write(tmp_path, _spec_doc(), stem="renamed")
    with pytest.raises(ValueError, match="must match the file name"):
        load_model_spec(path)


def test_new_model_by_copying_a_spec(tmp_path):
    doc = _spec_doc("llama-3.1-8b-instruct-fp8")
    doc.update(name="llama-3.1-8b-awq")
    doc["serving"].update(quantization="awq", weights_gb=5.7)
    _write(tmp_path, doc)
    assert list_model_specs(tmp_path) == ["llama-3.1-8b-awq"]
    s = load_model_spec("llama-3.1-8b-awq", tmp_path)
    args = s.vllm_serve_args(port=8001)
    assert args[args.index("--quantization") + 1] == "awq"
    assert "--gpu-memory-utilization" not in args  # left to the runner when the spec says null


# --------------------------------------------------------------------------- validation


def test_qwen3_requires_explicit_thinking_choice():
    doc = _spec_doc()
    doc["request"]["chat_template_kwargs"] = {}
    with pytest.raises(ValidationError, match="enable_thinking"):
        ModelSpec.model_validate(doc)


def test_qwen3_thinking_mode_allowed_when_explicit():
    doc = _spec_doc()
    doc["request"]["chat_template_kwargs"] = {"enable_thinking": True}
    assert ModelSpec.model_validate(doc).request.chat_template_kwargs["enable_thinking"] is True


def test_unknown_field_rejected():
    doc = _spec_doc()
    doc["serving"]["max_model_length"] = 4096
    with pytest.raises(ValidationError):
        ModelSpec.model_validate(doc)


@pytest.mark.parametrize("flag", ["--port", "--max-model-len=4096", "--seed", "--gpu-memory-utilization"])
def test_extra_args_cannot_override_managed_flags(flag):
    doc = _spec_doc()
    doc["serving"]["extra_args"] = [flag]
    with pytest.raises(ValidationError, match="managed by the spec"):
        ModelSpec.model_validate(doc)


def test_bad_name_rejected():
    doc = _spec_doc()
    doc["name"] = "Qwen3 8B"
    with pytest.raises(ValidationError):
        ModelSpec.model_validate(doc)


# --------------------------------------------------------------------------- derived values


def test_revision_pinning():
    doc = _spec_doc()
    assert not ModelSpec.model_validate(doc).revision_pinned
    doc["model"]["revision"] = "a" * 40
    assert ModelSpec.model_validate(doc).revision_pinned


def test_vllm_serve_args():
    s = load_model_spec("qwen3-8b-fp8", MODELS)
    assert s.vllm_serve_args(port=8002, gpu_memory_utilization=0.9) == [
        "vllm", "serve", "Qwen/Qwen3-8B",
        "--revision", "main",
        "--served-model-name", "qwen3-8b-fp8",
        "--dtype", "bfloat16",
        "--max-model-len", "8192",
        "--host", "127.0.0.1",
        "--port", "8002",
        "--quantization", "fp8",
        "--gpu-memory-utilization", "0.9",
        "--seed", "1234",
    ]  # fmt: skip


def test_request_params_for_llama_have_no_extra_body():
    params = load_model_spec("llama-3.1-8b-instruct-fp8", MODELS).request_params()
    assert params == {"model": "llama-3.1-8b-instruct-fp8", "temperature": 0.0, "top_p": 1.0, "max_tokens": 512, "seed": 1234}
