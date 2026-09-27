import json
import socket
import threading
from pathlib import Path

import pytest

from release_gate.cli import main
from release_gate.mock import PERSONAS, MockBackend, make_server

ROOT = Path(__file__).resolve().parents[1]


def _backend(served_model: str) -> MockBackend:
    persona = PERSONAS["reference"].model_copy(update={"base_latency_ms": 1.0, "jitter_ms": 1.0})
    return MockBackend(persona, served_model)


def _start_server(backend: MockBackend) -> tuple[object, threading.Thread, int]:
    server = make_server(backend, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, server.server_address[1]


def _args(
    baseline_port: int,
    candidate_port: int,
    runs_root: Path,
    env_file: Path,
    *,
    run_id: str = "e2e",
) -> list[str]:
    return [
        "run",
        "--suite",
        str(ROOT / "suites/starter-v1"),
        "--prompt",
        str(ROOT / "prompts/triage-v1.yaml"),
        "--models-dir",
        str(ROOT / "models"),
        "--baseline",
        "llama-3.1-8b-instruct-fp8",
        "--candidate",
        "qwen3-8b-fp8",
        "--baseline-url",
        f"http://127.0.0.1:{baseline_port}/v1",
        "--candidate-url",
        f"http://127.0.0.1:{candidate_port}/v1",
        "--warmup",
        "0",
        "--runs-root",
        str(runs_root),
        "--run-id",
        run_id,
        "--env-file",
        str(env_file),
    ]


def test_run_cli_executes_suite_and_saves_environment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline, _, baseline_port = _start_server(_backend("llama-3.1-8b-instruct-fp8"))
    candidate, _, candidate_port = _start_server(_backend("qwen3-8b-fp8"))
    env_file = tmp_path / "env.json"
    env_file.write_text('{"gpu": "none"}', encoding="utf-8")
    args = _args(baseline_port, candidate_port, tmp_path, env_file)
    try:
        assert main(args) == 0
        assert "(30 results)" in capsys.readouterr().out
        results = tmp_path / "e2e" / "results.jsonl"
        assert len(results.read_text(encoding="utf-8").splitlines()) == 30
        manifest = json.loads((tmp_path / "e2e" / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["environment"]["extra"] == {"gpu": "none"}

        assert main(args) == 4
        assert "already exists" in capsys.readouterr().err
    finally:
        baseline.shutdown()
        baseline.server_close()
        candidate.shutdown()
        candidate.server_close()


def test_run_cli_reports_endpoint_preflight_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    baseline, _, baseline_port = _start_server(_backend("llama-3.1-8b-instruct-fp8"))
    unavailable = socket.socket()
    unavailable.bind(("127.0.0.1", 0))
    candidate_port = unavailable.getsockname()[1]
    unavailable.close()
    env_file = tmp_path / "env.json"
    env_file.write_text("{}", encoding="utf-8")
    try:
        assert main(_args(baseline_port, candidate_port, tmp_path, env_file, run_id="preflight")) == 4
        assert "preflight" in capsys.readouterr().err
    finally:
        baseline.shutdown()
        baseline.server_close()


def test_run_cli_rejects_non_string_environment_values(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env_file = tmp_path / "env.json"
    env_file.write_text("[1, 2]", encoding="utf-8")

    assert (
        main(
            [
                "run",
                "--suite",
                "suite",
                "--baseline",
                "base",
                "--candidate",
                "candidate",
                "--baseline-url",
                "http://127.0.0.1:8001/v1",
                "--candidate-url",
                "http://127.0.0.1:8002/v1",
                "--env-file",
                str(env_file),
            ]
        )
        == 4
    )
    assert "JSON object with string keys and values" in capsys.readouterr().err


def test_run_cli_missing_arguments_use_usage_exit_code() -> None:
    with pytest.raises(SystemExit) as error:
        main(["run"])

    assert error.value.code == 4
