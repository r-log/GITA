"""LLM client abstraction.

Exposes a small ``LLMClient`` protocol that every agent uses, with two
implementations:

- ``OpenRouterClient`` — HTTP calls to OpenRouter's chat-completions API,
  with schema-constrained JSON output, retries for transient failures,
  and typed errors for the failure shapes that matter.
- ``FakeLLMClient`` — canned responses from a queue, used throughout the
  test suite so pytest never calls a provider.

Error taxonomy (all subclass ``LLMError``):

- ``LLMTransientError`` — 429, 5xx, timeouts, connection failures. Already
  retried here with backoff; callers may retry the whole job later.
- ``LLMRequestError`` — any other non-2xx. Retrying won't help.
- ``LLMTruncatedError`` — the model hit ``max_tokens``; the JSON is cut off.
- ``LLMRefusalError`` — no content (refusal / content filter).
- ``LLMSchemaError`` — content arrived but doesn't match the schema.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_TIMEOUT = httpx.Timeout(180.0, connect=10.0)
_DEFAULT_MAX_RETRIES = 3
_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_CAP_SECONDS = 60.0
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(?P<body>.*?)\n?```\s*$", re.DOTALL)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class LLMError(RuntimeError):
    """Base class for LLM client failures."""


class LLMTransientError(LLMError):
    """Rate limit, server error, timeout, or connection failure."""

    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMRequestError(LLMError):
    """A non-retryable HTTP error (bad request, auth, unknown model...)."""

    def __init__(self, message: str, *, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class LLMTruncatedError(LLMError):
    """The response stopped at ``max_tokens``; structured output is incomplete."""


class LLMRefusalError(LLMError):
    """The provider returned no content (refusal or content filter)."""


class LLMSchemaError(LLMError):
    """The LLM returned JSON that didn't validate against the requested schema."""

    def __init__(self, schema_name: str, raw: str, validation_error: Exception):
        super().__init__(f"LLM response did not validate against {schema_name}: {validation_error}")
        self.schema_name = schema_name
        self.raw = raw
        self.validation_error = validation_error


# ---------------------------------------------------------------------------
# Structured-output helpers
# ---------------------------------------------------------------------------
def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """A JSON schema that strict structured-output modes accept.

    Strict modes require every object to list *all* its properties as
    required and to set ``additionalProperties: false``. Pydantic leaves
    fields with defaults out of ``required``; this closes that gap so the
    provider actually constrains decoding instead of rejecting or
    ignoring the schema.
    """
    schema = copy.deepcopy(model.model_json_schema())

    def close(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            for value in node.values():
                close(value)
        elif isinstance(node, list):
            for item in node:
                close(item)

    close(schema)
    return schema


def extract_json_text(content: str) -> str:
    """Strip a markdown code fence some models wrap around JSON."""
    match = _FENCE_RE.match(content)
    return match.group("body") if match else content


def _parse_structured(content: str, schema: type[BaseModel]) -> BaseModel:
    try:
        return schema.model_validate_json(extract_json_text(content))
    except ValidationError as exc:
        raise LLMSchemaError(
            schema_name=schema.__name__, raw=content, validation_error=exc
        ) from exc


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, min(float(value), _BACKOFF_CAP_SECONDS))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
@dataclass
class LLMResponse:
    """What an LLM call returns."""

    content: str  # raw content returned by the model
    parsed: BaseModel | None  # populated iff response_schema was provided
    model: str
    usage: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------
class LLMClient(Protocol):
    """Every agent talks to the LLM through this narrow interface.

    If ``response_schema`` is provided, the implementation must validate the
    returned JSON against it and populate ``LLMResponse.parsed``. On
    validation failure it must raise ``LLMSchemaError``.

    ``temperature`` defaults to ``0.0`` so structured-output calls are as
    deterministic as the model allows. Week 3 Day 4 observed that
    title-based dedupe signatures miss when the LLM rephrases equivalent
    milestones between runs; dropping the temperature is the cheapest fix
    that tightens variance without blocking exploratory prompts from
    overriding per-call.
    """

    async def call(
        self,
        *,
        system: str,
        user: str,
        response_schema: type[BaseModel] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# OpenRouter implementation
# ---------------------------------------------------------------------------
class OpenRouterClient:
    """HTTPX client for OpenRouter's chat/completions endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        default_model: str = "moonshotai/kimi-k2.5",
        http: httpx.AsyncClient | None = None,
        base_url: str = OPENROUTER_URL,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self.api_key = api_key
        self.default_model = default_model
        self.base_url = base_url
        self.max_retries = max_retries
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def __aenter__(self) -> OpenRouterClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def _post_once(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self.http.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise LLMTransientError(f"transport error: {exc!r}") from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise LLMTransientError(
                f"HTTP {response.status_code} from provider",
                retry_after=_retry_after_seconds(response),
            )
        if response.status_code >= 400:
            raise LLMRequestError(
                f"HTTP {response.status_code}: {response.text[:300]}",
                status_code=response.status_code,
            )
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise LLMTransientError("provider returned a non-JSON body") from exc

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST with jittered exponential backoff on transient errors."""
        attempt = 0
        while True:
            try:
                return await self._post_once(payload)
            except LLMTransientError as exc:
                if attempt >= self.max_retries:
                    raise
                delay = exc.retry_after
                if delay is None:
                    delay = min(
                        _BACKOFF_CAP_SECONDS,
                        _BACKOFF_BASE_SECONDS * 2**attempt,
                    ) * random.uniform(0.5, 1.0)
                logger.warning(
                    "llm_retry attempt=%d delay=%.1fs reason=%s",
                    attempt + 1,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                attempt += 1

    async def call(
        self,
        *,
        system: str,
        user: str,
        response_schema: type[BaseModel] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        model_name = model or self.default_model
        payload: dict[str, Any] = {
            "model": model_name,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if response_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_schema.__name__,
                    "strict": True,
                    "schema": strict_json_schema(response_schema),
                },
            }

        data = await self._post(payload)

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"malformed OpenRouter response: {exc} (raw={data!r:.200})") from exc

        usage = data.get("usage", {}) or {}
        logger.info(
            "llm_call model=%s input_tokens=%s output_tokens=%s finish=%s schema=%s",
            model_name,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            finish_reason,
            response_schema.__name__ if response_schema else None,
        )

        if finish_reason == "length":
            raise LLMTruncatedError(
                f"response hit max_tokens={max_tokens} (model={model_name}); "
                "raise max_tokens or shrink the prompt"
            )
        if content is None or finish_reason == "content_filter":
            raise LLMRefusalError(f"no content returned (finish_reason={finish_reason!r})")

        parsed = (
            _parse_structured(content, response_schema) if response_schema is not None else None
        )
        return LLMResponse(
            content=content,
            parsed=parsed,
            model=model_name,
            usage=usage,
        )


# ---------------------------------------------------------------------------
# Fake implementation (used throughout the test suite)
# ---------------------------------------------------------------------------
class FakeLLMClient:
    """Returns canned responses from a queue, one per ``call()``.

    Responses can be passed as:
      - ``BaseModel`` instances: serialized to JSON and returned as content,
        also validated against the requested schema (consistency check).
      - ``str``: returned as-is (useful for testing non-schema calls).
    """

    def __init__(self, responses: list[BaseModel | str]) -> None:
        self._responses: list[BaseModel | str] = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def call(
        self,
        *,
        system: str,
        user: str,
        response_schema: type[BaseModel] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "schema": (response_schema.__name__ if response_schema else None),
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )

        if not self._responses:
            raise LLMError("FakeLLMClient ran out of canned responses")

        canned = self._responses.pop(0)
        if isinstance(canned, BaseModel):
            content = canned.model_dump_json()
            parsed: BaseModel | None = canned
            # If a schema was requested, check that the canned response matches.
            if response_schema is not None and not isinstance(canned, response_schema):
                try:
                    parsed = response_schema.model_validate_json(content)
                except ValidationError as exc:
                    raise LLMSchemaError(
                        schema_name=response_schema.__name__,
                        raw=content,
                        validation_error=exc,
                    ) from exc
        else:
            content = canned
            parsed = None
            if response_schema is not None:
                try:
                    parsed = response_schema.model_validate_json(content)
                except ValidationError as exc:
                    raise LLMSchemaError(
                        schema_name=response_schema.__name__,
                        raw=content,
                        validation_error=exc,
                    ) from exc

        return LLMResponse(
            content=content,
            parsed=parsed,
            model=model or "fake",
            usage={},
        )
