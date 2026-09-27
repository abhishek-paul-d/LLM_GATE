"""Local threaded HTTP transport for the deterministic mock backend."""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

from .model import MockBackend

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost"})


def make_server(backend: MockBackend, host: str = "127.0.0.1", port: int = 8001) -> ThreadingHTTPServer:
    """Create a threaded HTTP server that exposes a ``MockBackend``. Only loopback hosts are allowed."""
    if host not in LOOPBACK_HOSTS:
        raise ValueError(f"mock server binds loopback only ({', '.join(sorted(LOOPBACK_HOSTS))}), got {host!r}")

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if length < 0:
                status, payload, delay = 400, {"error": {"message": "invalid Content-Length"}}, 0.0
            else:
                body = self.rfile.read(length) if length else b""
                with self.server.backend_lock:  # type: ignore[attr-defined]
                    status, payload, delay = backend.handle(self.command, self.path, body)
            if delay > 0:
                time.sleep(delay)
            response = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = _Server((host, port), Handler)
    server.backend_lock = Lock()  # type: ignore[attr-defined]
    return server


class _Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind a port that is already listening, and
    # requests then go to either one. Elsewhere it only permits rebinding after TIME_WAIT.
    allow_reuse_address = sys.platform != "win32"


def serve(backend: MockBackend, host: str = "127.0.0.1", port: int = 8001) -> None:
    """Serve a ``MockBackend`` until the process is interrupted."""

    make_server(backend, host, port).serve_forever()
