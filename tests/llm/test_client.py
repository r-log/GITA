"""Tests for the LLM client layer.

Every test uses either the FakeLLMClient or ``httpx.MockTransport`` — zero
real HTTP calls. If any test in this file tries to hit OpenRouter, the
transport will raise.
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from gita.llm.client import (
    FakeLLMClient,
    LLMError,
    LLMRefusalError,
    LLMRequestError,
    LLMSchemaError,
    LLMTransientError,
    LLMTruncatedError,
    OpenRouterClient,
    strict_json_schema,
)


# ---------------------------------------------------------------------------
# Canned schemas used by multiple tests
# ---------------------------------------------------------------------------
class SimpleResponse(BaseModel):
    label: str
    count: int


class NestedResponse(BaseModel):
    items: list[str]
    summary: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_chat_response(content: str, usage: dict | None = None) -> dict:
    return {
        "id": "gen-test",
        "model": "fake-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 20},
    }


class Capture:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []


def _build_client(handler, capture: Capture | None = None) -> OpenRouterClient:
    if capture is not None:

        def wrapped(req: httpx.Request) -> httpx.Response:
            capture.requests.append(req)
            return handler(req)

        transport = httpx.MockTransport(wrapped)
    else:
        transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return OpenRouterClient(
        api_key="test-key",
        default_model="test/model",
        http=http,
    )


# ===========================================================================
# OpenRouterClient
# ===========================================================================
@pytest.fixture
def no_sleep(monkeypatch) -> list[float]:
    """Record backoff delays instead of sleeping."""
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("gita.llm.client.asyncio.sleep", fake_sleep)
    return delays


class TestOpenRouterBasic:
    async def test_plain_call_returns_content(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_chat_response("hello from the model"))

        client = _build_client(handler)
        result = await client.call(system="be helpful", user="hi")
        assert result.content == "hello from the model"
        assert result.parsed is None
        assert result.model == "test/model"

    async def test_request_body_contains_messages(self):
        capture = Capture()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_chat_response("ok"))

        client = _build_client(handler, capture)
        await client.call(
            system="sys prompt",
            user="user prompt",
            model="openai/gpt-4o",
            max_tokens=256,
        )
        req = capture.requests[0]
        body = json.loads(req.content)
        assert body["model"] == "openai/gpt-4o"
        assert body["max_tokens"] == 256
        assert body["messages"][0] == {
            "role": "system",
            "content": "sys prompt",
        }
        assert body["messages"][1] == {
            "role": "user",
            "content": "user prompt",
        }

    async def test_authorization_header_uses_api_key(self):
        capture = Capture()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_chat_response("ok"))

        client = _build_client(handler, capture)
        await client.call(system="x", user="y")
        req = capture.requests[0]
        assert req.headers["Authorization"] == "Bearer test-key"
        assert req.headers["Content-Type"] == "application/json"

    async def test_default_model_used_when_omitted(self):
        capture = Capture()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_chat_response("ok"))

        client = _build_client(handler, capture)
        await client.call(system="x", user="y")  # no model
        req = capture.requests[0]
        body = json.loads(req.content)
        assert body["model"] == "test/model"

    async def test_http_500_retried_then_transient_error(self, no_sleep):
        capture = Capture()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "down"})

        client = _build_client(handler, capture)
        with pytest.raises(LLMTransientError):
            await client.call(system="x", user="y")
        assert len(capture.requests) == client.max_retries + 1
        assert len(no_sleep) == client.max_retries

    async def test_usage_tokens_preserved(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_make_chat_response(
                    "ok",
                    usage={"prompt_tokens": 123, "completion_tokens": 456},
                ),
            )

        client = _build_client(handler)
        result = await client.call(system="x", user="y")
        assert result.usage["prompt_tokens"] == 123
        assert result.usage["completion_tokens"] == 456


class TestOpenRouterStructuredOutput:
    async def test_schema_call_parses_valid_json(self):
        payload = SimpleResponse(label="hi", count=3)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_chat_response(payload.model_dump_json()))

        client = _build_client(handler)
        result = await client.call(system="x", user="y", response_schema=SimpleResponse)
        assert isinstance(result.parsed, SimpleResponse)
        assert result.parsed.label == "hi"
        assert result.parsed.count == 3

    async def test_schema_request_includes_response_format(self):
        capture = Capture()
        payload = SimpleResponse(label="ok", count=1)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_chat_response(payload.model_dump_json()))

        client = _build_client(handler, capture)
        await client.call(system="x", user="y", response_schema=SimpleResponse)
        req = capture.requests[0]
        body = json.loads(req.content)
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["name"] == "SimpleResponse"
        assert body["response_format"]["json_schema"]["strict"] is True
        # The schema itself is attached
        assert "properties" in body["response_format"]["json_schema"]["schema"]

    async def test_invalid_json_raises_schema_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_chat_response("not json at all"))

        client = _build_client(handler)
        with pytest.raises(LLMSchemaError) as exc_info:
            await client.call(system="x", user="y", response_schema=SimpleResponse)
        assert exc_info.value.schema_name == "SimpleResponse"
        assert "not json at all" in exc_info.value.raw

    async def test_wrong_shape_raises_schema_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json=_make_chat_response(
                    json.dumps({"label": "hi"})  # missing `count`
                ),
            )

        client = _build_client(handler)
        with pytest.raises(LLMSchemaError):
            await client.call(system="x", user="y", response_schema=SimpleResponse)

    async def test_malformed_openrouter_response_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"not": "what we expected"})

        client = _build_client(handler)
        with pytest.raises(LLMError, match="malformed OpenRouter"):
            await client.call(system="x", user="y")


# ===========================================================================
# FakeLLMClient
# ===========================================================================
class TestFakeLLMClient:
    async def test_returns_canned_basemodel(self):
        fake = FakeLLMClient(responses=[SimpleResponse(label="canned", count=7)])
        result = await fake.call(system="x", user="y", response_schema=SimpleResponse)
        assert isinstance(result.parsed, SimpleResponse)
        assert result.parsed.label == "canned"

    async def test_captures_calls(self):
        fake = FakeLLMClient(responses=[SimpleResponse(label="a", count=1)])
        await fake.call(
            system="be helpful",
            user="do X",
            response_schema=SimpleResponse,
            model="claude-sonnet",
            max_tokens=999,
        )
        call = fake.calls[0]
        assert call["system"] == "be helpful"
        assert call["user"] == "do X"
        assert call["schema"] == "SimpleResponse"
        assert call["model"] == "claude-sonnet"
        assert call["max_tokens"] == 999

    async def test_queue_consumed_in_order(self):
        fake = FakeLLMClient(
            responses=[
                SimpleResponse(label="first", count=1),
                SimpleResponse(label="second", count=2),
            ]
        )
        r1 = await fake.call(system="x", user="y", response_schema=SimpleResponse)
        r2 = await fake.call(system="x", user="y", response_schema=SimpleResponse)
        assert r1.parsed.label == "first"
        assert r2.parsed.label == "second"

    async def test_empty_queue_raises(self):
        fake = FakeLLMClient(responses=[])
        with pytest.raises(LLMError, match="ran out of canned"):
            await fake.call(system="x", user="y")

    async def test_string_response_works_without_schema(self):
        fake = FakeLLMClient(responses=["raw text"])
        result = await fake.call(system="x", user="y")
        assert result.content == "raw text"
        assert result.parsed is None

    async def test_string_response_validates_against_schema(self):
        fake = FakeLLMClient(responses=[json.dumps({"label": "ok", "count": 5})])
        result = await fake.call(system="x", user="y", response_schema=SimpleResponse)
        assert isinstance(result.parsed, SimpleResponse)
        assert result.parsed.count == 5

    async def test_invalid_string_response_raises_schema_error(self):
        fake = FakeLLMClient(responses=["not json"])
        with pytest.raises(LLMSchemaError):
            await fake.call(system="x", user="y", response_schema=SimpleResponse)


# ===========================================================================
# Failure shapes
# ===========================================================================
class TestOpenRouterFailureShapes:
    async def test_429_honours_retry_after_then_succeeds(self, no_sleep):
        responses = iter(
            [
                httpx.Response(429, headers={"retry-after": "3"}, json={}),
                httpx.Response(200, json=_make_chat_response("ok")),
            ]
        )
        client = _build_client(lambda request: next(responses))
        result = await client.call(system="x", user="y")
        assert result.content == "ok"
        assert no_sleep == [3.0]

    async def test_transport_error_is_transient(self, no_sleep):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client = _build_client(handler)
        with pytest.raises(LLMTransientError):
            await client.call(system="x", user="y")

    async def test_400_is_not_retried(self, no_sleep):
        capture = Capture()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "unknown model"})

        client = _build_client(handler, capture)
        with pytest.raises(LLMRequestError) as info:
            await client.call(system="x", user="y")
        assert info.value.status_code == 400
        assert len(capture.requests) == 1 and no_sleep == []

    async def test_truncated_json_is_reported_as_truncation(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = _make_chat_response('{"label": "cut of')
            body["choices"][0]["finish_reason"] = "length"
            return httpx.Response(200, json=body)

        client = _build_client(handler)
        with pytest.raises(LLMTruncatedError, match="max_tokens"):
            await client.call(system="x", user="y", response_schema=SimpleResponse)

    async def test_missing_content_is_a_refusal(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = _make_chat_response("x")
            body["choices"][0]["message"]["content"] = None
            return httpx.Response(200, json=body)

        client = _build_client(handler)
        with pytest.raises(LLMRefusalError):
            await client.call(system="x", user="y")

    async def test_fenced_json_is_accepted(self):
        fenced = '```json\n{"label": "hi", "count": 3}\n```'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_chat_response(fenced))

        client = _build_client(handler)
        result = await client.call(system="x", user="y", response_schema=SimpleResponse)
        assert result.parsed == SimpleResponse(label="hi", count=3)


class TestStrictJsonSchema:
    def test_every_object_requires_all_properties_and_is_closed(self):
        from gita.agents.onboarding.schemas import FindingsResponse

        schema = strict_json_schema(FindingsResponse)
        finding = schema["$defs"]["LLMFinding"]
        assert finding["required"] == list(finding["properties"])  # incl. fix_sketch
        assert finding["additionalProperties"] is False
        assert schema["additionalProperties"] is False
        assert finding["properties"]["severity"]["enum"] == [
            "low",
            "medium",
            "high",
            "critical",
        ]

    async def test_sent_in_request(self):
        capture = Capture()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=_make_chat_response('{"label": "a", "count": 1}'))

        client = _build_client(handler, capture)
        await client.call(system="x", user="y", response_schema=SimpleResponse)
        sent = json.loads(capture.requests[0].content)["response_format"]["json_schema"]
        assert sent["strict"] is True
        assert sent["schema"] == strict_json_schema(SimpleResponse)
