import json
import socket
import threading

import httpx
import pytest

from release_gate.cli import main
from release_gate.mock import PERSONAS, MockBackend, make_server


def test_http_server_exposes_mock_backend() -> None:
    backend = MockBackend(PERSONAS["reference"].model_copy(update={"base_latency_ms": 1.0, "jitter_ms": 1.0}), "m")
    server = make_server(backend, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:
            models = client.get("/v1/models")
            assert models.status_code == 200
            assert models.json()["data"][0]["id"] == "m"

            completion = client.post(
                "/v1/chat/completions",
                json={
                    "model": "m",
                    "messages": [
                        {
                            "role": "user",
                            "content": "[ALERT] X\nstatus: firing\nmetrics:\n  http_5xx_rate: 7.0%\nlogs:\n",
                        }
                    ],
                },
            )
            assert completion.status_code == 200
            result = json.loads(completion.json()["choices"][0]["message"]["content"])
            assert result["severity"] == "high"

            wrong_model = client.post(
                "/v1/chat/completions",
                json={"model": "other", "messages": [{"role": "user", "content": "alert"}]},
            )
            assert wrong_model.status_code == 404
            assert client.get("/nope").status_code == 404
    finally:
        server.shutdown()
        server.server_close()


def test_mock_cli_invalid_persona_uses_usage_exit_code() -> None:
    with pytest.raises(SystemExit) as error:
        main(["mock", "serve", "--persona", "nope", "--served-model", "m"])

    assert error.value.code == 4


def test_server_refuses_non_loopback_hosts() -> None:
    with pytest.raises(ValueError, match="loopback"):
        make_server(MockBackend(PERSONAS["reference"], "m"), host="0.0.0.0", port=0)


def test_mock_cli_non_loopback_host_uses_usage_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["mock", "serve", "--persona", "reference", "--served-model", "m", "--host", "0.0.0.0"]) == 4
    assert "loopback" in capsys.readouterr().err


def test_busy_port_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    first = make_server(MockBackend(PERSONAS["reference"], "m"), port=0)
    port = str(first.server_address[1])
    try:
        assert main(["mock", "serve", "--persona", "reference", "--served-model", "m", "--port", port]) == 4
        assert capsys.readouterr().err.startswith("error:")
    finally:
        first.server_close()


def test_server_rejects_bad_content_length() -> None:
    server = make_server(MockBackend(PERSONAS["reference"], "m"), port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # httpx refuses to send a malformed header, so write the request by hand.
        with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=10) as conn:
            conn.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n")
            assert conn.recv(64).startswith(b"HTTP/1.0 400")
    finally:
        server.shutdown()
        server.server_close()
