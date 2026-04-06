"""Tests for src.tools.ai.code_analyzer — diff quality + test coverage checking."""

import json
from unittest.mock import AsyncMock, patch, MagicMock

from src.tools.ai.code_analyzer import (
    _analyze_diff_quality, _check_test_coverage,
    make_analyze_diff_quality, make_check_test_coverage,
)


def _mock_llm_response(content: str):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
    return response


class TestAnalyzeDiffQuality:
    @patch("src.tools.ai.code_analyzer._client")
    async def test_success(self, mock_client):
        result_json = json.dumps({"overall_quality": "good", "score": 8.5, "summary": "Clean code"})
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_llm_response(result_json))

        result = await _analyze_diff_quality("+def foo(): pass", {"files": []})
        assert result.success is True

    @patch("src.tools.ai.code_analyzer._client")
    async def test_error(self, mock_client):
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("err"))
        result = await _analyze_diff_quality("diff", {})
        assert result.success is False


class TestCheckTestCoverage:
    @patch("src.tools.ai.code_analyzer._client")
    async def test_success(self, mock_client):
        result_json = json.dumps({"has_tests": True, "coverage_assessment": "good", "summary": "Tests present"})
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_llm_response(result_json))

        result = await _check_test_coverage("+def test_foo(): pass", [{"filename": "test_main.py"}])
        assert result.success is True

    @patch("src.tools.ai.code_analyzer._client")
    async def test_error(self, mock_client):
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("err"))
        result = await _check_test_coverage("diff", [])
        assert result.success is False


class TestFactories:
    def test_make_analyze_diff_quality(self):
        assert make_analyze_diff_quality().name == "analyze_diff_quality"

    def test_make_check_test_coverage(self):
        assert make_check_test_coverage().name == "check_test_coverage"
