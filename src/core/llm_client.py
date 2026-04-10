"""
Centralized LLM client factory + helpers.

Every module that needs an LLM client calls get_llm_client() instead of
instantiating AsyncOpenAI directly. This ensures base_url, api_key, and
timeout are consistent and configurable from one place (.env).

Supports any OpenAI-compatible API: OpenRouter, Anthropic, Ollama, Kimi,
Together AI, vLLM, etc. Just change LLM_BASE_URL and LLM_API_KEY in .env.
"""

import json

import structlog
from openai import AsyncOpenAI

from src.core.config import settings

log = structlog.get_logger()

# Lazy singleton — created on first call, reused after
_client: AsyncOpenAI | None = None


def get_llm_client() -> AsyncOpenAI:
    """Get the shared LLM client. Thread-safe for async usage."""
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.effective_api_key,
            timeout=settings.llm_timeout,
        )
    return _client


async def llm_json_call(
    model: str,
    messages: list[dict],
    caller: str = "unknown",
    temperature: float = 0.2,
    max_retries: int = 2,
) -> dict | None:
    """
    Make an LLM call that expects JSON output, with retry on empty/invalid responses.

    Args:
        model: The model ID to use
        messages: The chat messages
        caller: Name of the calling function (for logging)
        temperature: LLM temperature
        max_retries: How many times to retry on empty/invalid JSON

    Returns:
        Parsed dict on success, None on failure after all retries
    """
    client = get_llm_client()

    for attempt in range(1, max_retries + 1):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                response_format={"type": "json_object"},
            )

            content = response.choices[0].message.content or ""
            if not content.strip():
                log.warning(
                    "llm_json_empty_response",
                    caller=caller,
                    attempt=attempt,
                    max_retries=max_retries,
                )
                if attempt < max_retries:
                    continue
                return None

            return json.loads(content)

        except json.JSONDecodeError as e:
            log.warning(
                "llm_json_parse_failed",
                caller=caller,
                attempt=attempt,
                error=str(e),
                raw_preview=content[:200] if content else "empty",
            )
            if attempt < max_retries:
                continue
            return None

        except Exception as e:
            log.warning(
                "llm_json_call_failed",
                caller=caller,
                attempt=attempt,
                error=str(e),
            )
            return None

    return None
