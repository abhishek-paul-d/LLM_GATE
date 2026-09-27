"""OpenAI-compatible adapter: parsing, error classification, and retries recorded per attempt."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from release_gate.adapters import ChatClient, chat_body
from release_gate.registry import load_model_spec

OK = {
    "model": "m",
    "choices": [{"message": {"role": "assistant", "content": '{"a": 1}'}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 4},
}


def _chat(responses: list, max_retries: int = 0):
    """Serve ``responses`` in order: an int status, a dict payload (200), bytes, or an exception."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        r = responses[min(len(calls), len(responses)) - 1]
        if isinstance(r, Exception):
            raise r
        if isinstance(r, int):
            return httpx.Response(r, json={"error": "x"})
        if isinstance(r, bytes):
            return httpx.Response(200, content=r)
        return httpx.Response(200, json=r)

    async def go():
        async with ChatClient(
            "http://test/v1", max_retries=max_retries, backoff_s=0, transport=httpx.MockTransport(handler)
        ) as c:
            return await c.chat({"model": "m", "messages": []})

    return asyncio.run(go()), calls


def test_success_is_parsed():
    c, calls = _chat([OK])
    assert (c.text, c.finish_reason, c.served_model, c.prompt_tokens, c.completion_tokens) == ('{"a": 1}', "stop", "m", 10, 4)
    assert c.error is None and c.first_error is None and len(c.attempts) == 1
    assert calls[0].url.path == "/v1/chat/completions"


def test_retry_rescues_but_first_error_is_kept():
    c, calls = _chat([500, OK], max_retries=1)
    assert c.text == '{"a": 1}' and c.error is None
    assert c.first_error == "http_5xx" and len(calls) == 2


def test_exhausted_retries_return_no_text():
    c, _ = _chat([500, 500], max_retries=1)
    assert c.text is None and c.error == "http_5xx" and len(c.attempts) == 2


@pytest.mark.parametrize(
    ("response", "kind", "retried"),
    [
        (429, "rate_limited", True),
        (503, "http_5xx", True),
        (400, "http_4xx", False),
        (b"not json", "invalid_response", False),
        ({"choices": []}, "invalid_response", False),
        (httpx.ReadTimeout("slow"), "timeout", True),
        (httpx.ConnectError("refused"), "transport", True),
    ],
)
def test_error_classification(response, kind, retried):
    c, calls = _chat([response], max_retries=1)
    assert c.first_error == kind and c.error == kind
    assert len(calls) == (2 if retried else 1)


def test_null_content_is_empty_text_not_an_error():
    c, _ = _chat([{"choices": [{"message": {"content": None}, "finish_reason": "length"}]}])
    assert c.text == "" and c.error is None and c.finish_reason == "length"


def test_chat_body_flattens_vllm_fields_and_sets_schema():
    spec = load_model_spec("qwen3-8b-fp8")
    body = chat_body(spec, [{"role": "user", "content": "x"}])
    assert body["model"] == "qwen3-8b-fp8" and body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "extra_body" not in body and "response_format" not in body
    constrained = spec.model_copy(update={"request": spec.request.model_copy(update={"structured_output": "json_schema"})})
    rf = chat_body(constrained, [])["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["schema"]["additionalProperties"] is False
    json.dumps(body)  # serialisable
