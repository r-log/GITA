"""LLM client abstraction.

Exposes a small ``LLMClient`` protocol that the onboarding agent (and any
future agent) uses, with two concrete implementations:

- ``OpenRouterClient`` — real HTTP calls via ``httpx``. Used in production
  and for Day 6 prompt iteration. Supports pydantic-schema-validated JSON
  responses via OpenRouter's ``response_format`` field.
- ``FakeLLMClient`` — returns canned responses from a queue. Used throughout
  the test suite so we never touch OpenRouter from pytest.

**No retry logic in Day 5.** If the LLM returns malformed JSON, we raise
``LLMSchemaError`` and let the caller decide. Day 6 adds retry-on-drift if
prompt iteration surfaces a need for it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_TIMEOUT = httpx.Timeout(180.0)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------
class LLMError(RuntimeError):
    """Base class for LLM client failures."""


class LLMSchemaError(LLMError):
    """The LLM returned JSON that didn't validate against the requested schema."""

    def __init__(self, schema_name: str, raw: str, validation_error: Exception):
        super().__init__(
            f"LLM response did not validate against {schema_name}: "
            f"{validation_error}"
        )
        self.schema_name = schema_name
        self.raw = raw
        self.validation_error = validation_error


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

    ``temperature`` defaults to a low value so structured-output calls are
    as deterministic as the model allows. Override per-call for creative
    tasks.
    """

    async def call(
        self,
        *,
        system: str,
        user: str,
        response_schema: type[BaseModel] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
    ) -> LLMResponse:
        ...


# ---------------------------------------------------------------------------
# OpenRouter implementation
# ---------------------------------------------------------------------------
class OpenRouterClient:
    """Thin HTTPX wrapper around OpenRouter's chat/completions endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        default_model: str = "moonshotai/kimi-k2.5",
        http: httpx.AsyncClient | None = None,
        base_url: str = OPENROUTER_URL,
    ) -> None:
        self.api_key = api_key
        self.default_model = default_model
        self.base_url = base_url
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def __aenter__(self) -> "OpenRouterClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def call(
        self,
        *,
        system: str,
        user: str,
        response_schema: type[BaseModel] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
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
                    "schema": response_schema.model_json_schema(),
                },
            }

        response = await self.http.post(
            self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(
                f"malformed OpenRouter response: {exc} (raw={data!r:.200})"
            ) from exc

        parsed: BaseModel | None = None
        if response_schema is not None:
            try:
                parsed = response_schema.model_validate_json(content)
            except ValidationError as exc:
                raise LLMSchemaError(
                    schema_name=response_schema.__name__,
                    raw=content,
                    validation_error=exc,
                ) from exc

        usage = data.get("usage", {}) or {}
        logger.info(
            "llm_call model=%s input_tokens=%s output_tokens=%s schema=%s",
            model_name,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            response_schema.__name__ if response_schema else None,
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
        temperature: float = 0.1,
    ) -> LLMResponse:
        self.calls.append(
            {
                "system": system,
                "user": user,
                "schema": (
                    response_schema.__name__ if response_schema else None
                ),
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
            if (
                response_schema is not None
                and not isinstance(canned, response_schema)
            ):
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
