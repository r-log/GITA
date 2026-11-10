"""Job retry policy, Redis URL handling and logging setup."""
from __future__ import annotations

import logging

import httpx
import pytest
from arq import Retry

from gita.jobs import _with_retry, is_transient
from gita.llm.client import LLMSchemaError, LLMTransientError, LLMTruncatedError
from gita.logging_config import configure_logging
from gita.worker import _mask_url, _parse_redis_url


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.github.com/x")
    return httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (LLMTransientError("429"), True),
        (_status_error(502), True),
        (_status_error(429), True),
        (httpx.ConnectTimeout("t"), True),
        (_status_error(404), False),
        (LLMTruncatedError("cut"), False),
        (LLMSchemaError("S", "raw", ValueError("bad")), False),
        (ValueError("bug"), False),
    ],
)
def test_is_transient(exc, expected):
    assert is_transient(exc) is expected


async def _fail(exc: BaseException):
    raise exc


class TestWithRetry:
    async def test_transient_failure_becomes_arq_retry_with_backoff(self):
        with pytest.raises(Retry) as first:
            await _with_retry({"job_try": 1}, lambda: _fail(LLMTransientError("502")))
        with pytest.raises(Retry) as second:
            await _with_retry({"job_try": 2}, lambda: _fail(LLMTransientError("502")))
        assert first.value.defer_score == 30_000
        assert second.value.defer_score == 60_000

    async def test_last_attempt_raises_the_real_error(self):
        with pytest.raises(LLMTransientError):
            await _with_retry({"job_try": 3}, lambda: _fail(LLMTransientError("502")))

    async def test_permanent_failure_is_not_retried(self):
        with pytest.raises(LLMTruncatedError):
            await _with_retry({"job_try": 1}, lambda: _fail(LLMTruncatedError("cut")))

    async def test_success_passes_through(self):
        async def ok():
            return {"status": "completed"}

        assert await _with_retry({"job_try": 1}, ok) == {"status": "completed"}


class TestRedisUrl:
    def test_rediss_enables_tls(self):
        """The hand-rolled parser dropped the scheme and connected in plaintext."""
        assert _parse_redis_url("rediss://:pw@cache.example.com:6380/2").ssl is True
        assert _parse_redis_url("redis://localhost:6379").ssl is False

    def test_mask_url_without_port(self):
        assert _mask_url("redis://:secret@cache.example.com") == "redis://:***@cache.example.com"


def test_configure_logging_enables_gita_info(monkeypatch):
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    configure_logging("info")
    assert root.handlers, "a handler must be attached to the root logger"
    assert logging.getLogger("gita.agents.decisions").isEnabledFor(logging.INFO)
    root.handlers.clear()
