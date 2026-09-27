"""A deterministic stand-in for an LLM behind an OpenAI-compatible API.

It triages alerts with keyword rules and the severity table, then degrades its answers
according to a ``Persona`` (wrong categories, unsafe commands, broken JSON, obeying
injected instructions, HTTP errors, latency). Every choice is a hash of the persona and
the input, so the same request always gets the same answer and a run is repeatable.

Used for development, tests and CI: it exercises the runner, scorers, statistics and gate
without a GPU. Its triage quality is deliberately crude and means nothing about any model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections import Counter
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..generator.severity import severity_for

Rate = float


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    base_latency_ms: float = Field(default=300.0, ge=0)
    jitter_ms: float = Field(default=100.0, ge=0)
    wrong_category_rate: Rate = Field(default=0.0, ge=0, le=1)
    unsafe_command_rate: Rate = Field(default=0.0, ge=0, le=1)
    invalid_json_rate: Rate = Field(default=0.0, ge=0, le=1)
    # Fraction of requests whose FIRST attempt returns HTTP 500; a retry succeeds.
    transient_error_rate: Rate = Field(default=0.0, ge=0, le=1)
    follow_injection: bool = False


PERSONAS: dict[str, Persona] = {
    p.name: p
    for p in (
        Persona(name="reference"),
        Persona(name="regressed-quality", wrong_category_rate=0.35),
        Persona(name="slow", base_latency_ms=900.0, jitter_ms=300.0),
        Persona(name="unsafe", unsafe_command_rate=0.2),
        Persona(name="obedient", follow_injection=True),
        Persona(name="flaky", transient_error_rate=0.2),
        Persona(name="broken-json", invalid_json_rate=0.3),
    )
}

CATEGORIES = ("capacity", "configuration", "dependency")

# Crude cause cues. Order matters only for the evidence phrase quoted in the summary.
_CUES: dict[str, tuple[str, ...]] = {
    "capacity": ("oomkilled", "out of memory", "throttl", "no space left", "ephemeral-storage", "max replicas"),
    "configuration": ("not set", "parse error", "imagepullbackoff", "errimagepull", "probe", "rolled out revision"),
    "dependency": ("timeout", "connection refused", "could not resolve", "lookup", "429", "quota exceeded"),
}
_INJECTION = re.compile(
    r"\b(severity|category)\b[^\n]*?\b(low|medium|high|capacity|configuration|dependency|unknown)\b", re.IGNORECASE
)


def _draw(persona: Persona, text: str, tag: str) -> float:
    digest = hashlib.sha256(f"{persona.name}\x1f{tag}\x1f{text}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _field(text: str, key: str) -> str | None:
    m = re.search(rf"^{key}: (\S+)", text, re.MULTILINE)
    return m.group(1) if m else None


def _severity(alert: str) -> str:
    if _field(alert, "status") == "resolved":
        return "low"
    if m := re.search(r"http_5xx_rate(?:_peak)?: ([\d.]+)%", alert):
        return severity_for("error_rate", float(m.group(1)))
    if m := re.search(r"container_restarts_1h(?:_peak)?: (\d+)", alert):
        return severity_for("restarts_1h", float(m.group(1)))
    if m := re.search(r"unavailable_replicas(?:_peak)?: (\d+) of (\d+)", alert):
        return severity_for("unavailable_replicas", float(m.group(1)), float(m.group(2)))
    return "medium"


def triage(alert: str, persona: Persona) -> str:
    """The mock's raw completion text for one alert."""
    log_lines = [line for line in alert.splitlines() if not _INJECTION.search(line)]
    body = "\n".join(log_lines).lower()
    found = {cat: next((cue for cue in cues if cue in body), None) for cat, cues in _CUES.items()}
    hits = [cat for cat, cue in found.items() if cue]
    category = hits[0] if len(hits) == 1 else "unknown"
    severity = _severity(alert)

    if category != "unknown" and _draw(persona, alert, "wrong") < persona.wrong_category_rate:
        others = [c for c in CATEGORIES if c != category]
        category = others[int(_draw(persona, alert, "which") * len(others))]
    if persona.follow_injection and (m := _INJECTION.search(alert)):
        field, value = m.group(1).lower(), m.group(2).lower()
        if field == "severity" and value in ("low", "medium", "high"):
            severity = value
        elif field == "category":
            category = value

    name = (_field(alert, "resource") or "deployment/unknown").removeprefix("deployment/")
    namespace = _field(alert, "namespace") or "default"
    if category == "unknown":
        summary = f"{name}: impact is visible but the alert does not show a single cause; need more logs"
    else:
        summary = f"{name}: {category} issue, evidence: {found.get(category) or 'log lines'}"
    next_check = f"kubectl describe deployment/{name} -n {namespace}"
    if _draw(persona, alert, "unsafe") < persona.unsafe_command_rate:
        next_check = f"kubectl rollout restart deployment/{name} -n {namespace}"

    record = json.dumps({"category": category, "severity": severity, "summary": summary, "next_check": next_check})
    if _draw(persona, alert, "json") < persona.invalid_json_rate:
        return "Here is the triage: " + record[: len(record) // 2]
    return record


class MockBackend:
    """Request handling shared by the in-process transport and the HTTP server.

    Stateful only in counting attempts per request body, so a transient error fails the
    first attempt and lets the retry succeed.
    """

    def __init__(self, persona: Persona, served_model: str) -> None:
        self.persona = persona
        self.served_model = served_model
        self._attempts: Counter[str] = Counter()

    def handle(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, Any], float]:
        """(status, JSON payload, delay in seconds)."""
        if method == "GET" and path.rstrip("/").endswith("/models"):
            return 200, {"object": "list", "data": [{"id": self.served_model, "object": "model"}]}, 0.0
        if method != "POST" or not path.rstrip("/").endswith("/chat/completions"):
            return 404, {"error": {"message": f"no route {method} {path}"}}, 0.0
        try:
            request = json.loads(body)
            alert = next(m["content"] for m in reversed(request["messages"]) if m.get("role") == "user")
        except (ValueError, KeyError, TypeError, StopIteration):
            return 400, {"error": {"message": "invalid chat completion request"}}, 0.0
        if request.get("model") != self.served_model:
            return 404, {"error": {"message": f"model {request.get('model')!r} not served"}}, 0.0

        p = self.persona
        delay = (p.base_latency_ms + p.jitter_ms * _draw(p, alert, "latency")) / 1000
        key = hashlib.sha256(body).hexdigest()
        self._attempts[key] += 1
        if self._attempts[key] == 1 and _draw(p, alert, "error") < p.transient_error_rate:
            return 500, {"error": {"message": "mock transient failure"}}, delay
        text = triage(alert, p)
        payload = {
            "id": f"mock-{key[:12]}",
            "object": "chat.completion",
            "created": 0,
            "model": self.served_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": len(alert) // 4,
                "completion_tokens": len(text) // 4,
                "total_tokens": len(alert) // 4 + len(text) // 4,
            },
        }
        return 200, payload, delay

    def transport(self) -> httpx.MockTransport:
        """In-process httpx transport: no sockets, real (scaled) latency via asyncio.sleep."""

        async def handler(request: httpx.Request) -> httpx.Response:
            status, payload, delay = self.handle(request.method, request.url.path, request.content)
            await asyncio.sleep(delay)
            return httpx.Response(status, json=payload)

        return httpx.MockTransport(handler)
