"""Tests for src.core.timing — async timing context manager."""

import pytest
from unittest.mock import patch, MagicMock

from src.core.timing import log_timing


class TestLogTiming:
    @patch("src.core.timing.log")
    async def test_logs_duration(self, mock_log):
        async with log_timing("test_op"):
            pass

        mock_log.info.assert_called_once()
        call_kwargs = mock_log.info.call_args
        assert call_kwargs[0][0] == "timing"
        assert call_kwargs[1]["operation"] == "test_op"
        assert "duration_ms" in call_kwargs[1]
        assert isinstance(call_kwargs[1]["duration_ms"], int)

    @patch("src.core.timing.log")
    async def test_exception_propagates_and_still_logs(self, mock_log):
        with pytest.raises(ValueError, match="boom"):
            async with log_timing("failing_op"):
                raise ValueError("boom")

        # Still logged despite exception
        mock_log.info.assert_called_once()
        assert mock_log.info.call_args[1]["operation"] == "failing_op"

    @patch("src.core.timing.log")
    async def test_context_kwargs_forwarded(self, mock_log):
        async with log_timing("llm_call", agent="pr_reviewer", model="claude-sonnet"):
            pass

        call_kwargs = mock_log.info.call_args[1]
        assert call_kwargs["agent"] == "pr_reviewer"
        assert call_kwargs["model"] == "claude-sonnet"
