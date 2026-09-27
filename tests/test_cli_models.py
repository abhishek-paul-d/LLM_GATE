import json
from pathlib import Path

import pytest

from release_gate.cli import main

ROOT = Path(__file__).resolve().parents[1]
MODELS = ROOT / "models"


def test_models_list_prints_shipped_specs(capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["models", "list", "--models-dir", str(MODELS)])
    captured = capsys.readouterr()

    assert result == 0
    lines = captured.out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("llama-3.1-8b-instruct-fp8\tmeta-llama/Llama-3.1-8B-Instruct\tmain\tunpinned\tgated")
    assert lines[1].startswith("qwen3-8b-fp8\tQwen/Qwen3-8B\tmain\tunpinned\topen")
    assert captured.err == ""


def test_models_show_prints_spec_json(capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["models", "show", "qwen3-8b-fp8", "--models-dir", str(MODELS)])
    captured = capsys.readouterr()

    assert result == 0
    assert json.loads(captured.out)["request"]["chat_template_kwargs"]["enable_thinking"] is False
    assert captured.err == ""


def test_models_serve_cmd_prints_vllm_command(capsys: pytest.CaptureFixture[str]) -> None:
    result = main(
        [
            "models",
            "serve-cmd",
            "qwen3-8b-fp8",
            "--port",
            "8002",
            "--gpu-memory-utilization",
            "0.9",
            "--models-dir",
            str(MODELS),
        ]
    )
    output = capsys.readouterr().out.strip()

    assert result == 0
    assert output.startswith("vllm serve Qwen/Qwen3-8B --revision main")
    assert "--port 8002" in output
    assert "--quantization fp8" in output
    assert "--gpu-memory-utilization 0.9" in output


def test_models_show_missing_spec_returns_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["models", "show", "does-not-exist", "--models-dir", str(MODELS)])

    assert result == 4
    assert capsys.readouterr().err.startswith("error:")


def test_models_show_rejects_name_that_does_not_match_file_stem(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = (MODELS / "qwen3-8b-fp8.yaml").read_text(encoding="utf-8")
    spec_path = tmp_path / "mismatched.yaml"
    spec_path.write_text(source.replace("name: qwen3-8b-fp8", "name: some-other-name", 1), encoding="utf-8")

    result = main(["models", "show", str(spec_path)])

    assert result == 4
    assert capsys.readouterr().err.startswith("error:")


def test_models_without_subcommand_uses_exit_code_4(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main(["models"])

    assert error.value.code == 4
    assert "usage:" in capsys.readouterr().err


def test_models_list_empty_directory_returns_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    result = main(["models", "list", "--models-dir", str(tmp_path)])
    captured = capsys.readouterr()

    assert result == 0
    assert captured.out == ""
    assert captured.err == f"no model specs in {tmp_path}\n"
