"""Async client for OpenAI-compatible ``/v1/chat/completions`` endpoints (vLLM, the local mock).

Every attempt is recorded, so the gate can measure errors **before** retries (plan.md §8):
retries may rescue a request, but they never hide that it failed first.

Error kinds:
- ``transport``: the endpoint could not be reached or the connection broke. Counted as an
  infrastructure error (run validity), not as a model regression.
- ``timeout``: no complete response within ``timeout_s``. A serving metric.
- ``rate_limited`` (HTTP 429), ``http_5xx``, ``http_4xx``: HTTP failures.
- ``invalid_response``: HTTP 200 whose body is not a chat completion.

Model output is returned as text and never interpreted here.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict

from ..registry import ModelSpec
from ..schemas.triage import triage_json_schema

ErrorKind = Literal["transport", "timeout", "rate_limited", "http_5xx", "http_4xx", "invalid_response"]
RETRYABLE: frozenset[str] = frozenset({"transport", "timeout", "rate_limited", "http_5xx"})


class Attempt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ErrorKind | None = None
    status_code: int | None = None
    latency_ms: float
    detail: str = ""


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str | None = None  # None when every attempt failed
    finish_reason: str | None = None
    served_model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    attempts: list[Attempt]
    total_ms: float  # wall time including retries and backoff

    @property
    def error(self) -> ErrorKind | None:
        return self.attempts[-1].error

    @property
    def first_error(self) -> ErrorKind | None:
        return self.attempts[0].error

    @property
    def latency_ms(self) -> float:
        """Wall time of the final attempt."""
        return self.attempts[-1].latency_ms


def chat_body(spec: ModelSpec, messages: list[dict[str, str]]) -> dict[str, Any]:
    """Request body for one case: the spec's sampling settings plus the messages.

    ``extra_body`` fields (vLLM-only, e.g. ``chat_template_kwargs``) are sent at the top level,
    which is what the OpenAI SDK does with them.
    """
    params = spec.request_params()
    body: dict[str, Any] = {k: v for k, v in params.items() if k != "extra_body"}
    body.update(params.get("extra_body", {}))
    body["messages"] = messages
    if spec.request.structured_output == "json_schema":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "triage_record", "schema": triage_json_schema(), "strict": True},
        }
    return body


def _parse(payload: Any) -> tuple[str, str | None, str | None, int | None, int | None]:
    """(text, finish_reason, model, prompt_tokens, completion_tokens); raises ValueError if malformed."""
    if not isinstance(payload, dict):
        raise ValueError("response body is not a JSON object")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("response has no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise ValueError("choice has no message")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ValueError("message content is not a string")
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}

    def count(key: str) -> int | None:
        value = usage.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    model = payload.get("model") if isinstance(payload.get("model"), str) else None
    return content or "", choices[0].get("finish_reason"), model, count("prompt_tokens"), count("completion_tokens")


class ChatClient:
    """One endpoint. Use as ``async with ChatClient(base_url) as client:``."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_s: float = 60.0,
        max_retries: int = 0,
        backoff_s: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_s = backoff_s
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> ChatClient:
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=httpx.Timeout(self.timeout_s), transport=self._transport)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ChatClient must be used inside 'async with'")
        return self._client

    async def list_models(self) -> list[str]:
        """Model ids served at ``/models``. Raises ``httpx.HTTPError`` or ``ValueError`` on failure."""
        resp = await self.client.get("/models")
        resp.raise_for_status()
        data = resp.json().get("data")
        if not isinstance(data, list):
            raise ValueError("/models response has no data list")
        return [m["id"] for m in data if isinstance(m, dict) and isinstance(m.get("id"), str)]

    async def chat(self, body: dict[str, Any]) -> Completion:
        attempts: list[Attempt] = []
        start = time.perf_counter()
        for i in range(self.max_retries + 1):
            if i:
                await asyncio.sleep(self.backoff_s * 2 ** (i - 1))
            attempt, parsed = await self._attempt(body)
            attempts.append(attempt)
            if attempt.error is None or attempt.error not in RETRYABLE:
                break
        total_ms = (time.perf_counter() - start) * 1000
        if attempts[-1].error is not None or parsed is None:
            return Completion(attempts=attempts, total_ms=total_ms)
        text, finish, model, p_tok, c_tok = parsed
        return Completion(
            text=text,
            finish_reason=finish,
            served_model=model,
            prompt_tokens=p_tok,
            completion_tokens=c_tok,
            attempts=attempts,
            total_ms=total_ms,
        )

    async def _attempt(self, body: dict[str, Any]) -> tuple[Attempt, tuple | None]:
        t0 = time.perf_counter()

        def done(error: ErrorKind | None = None, status: int | None = None, detail: str = "") -> Attempt:
            return Attempt(error=error, status_code=status, latency_ms=(time.perf_counter() - t0) * 1000, detail=detail[:200])

        try:
            resp = await self.client.post("/chat/completions", json=body)
        except httpx.TimeoutException as exc:
            return done("timeout", detail=type(exc).__name__), None
        except httpx.TransportError as exc:
            return done("transport", detail=f"{type(exc).__name__}: {exc}"), None
        status = resp.status_code
        if status == 429:
            return done("rate_limited", status), None
        if status >= 500:
            return done("http_5xx", status, resp.text), None
        if status >= 400:
            return done("http_4xx", status, resp.text), None
        try:
            parsed = _parse(resp.json())
        except ValueError as exc:  # includes JSONDecodeError
            return done("invalid_response", status, str(exc)), None
        return done(status=status), parsed
