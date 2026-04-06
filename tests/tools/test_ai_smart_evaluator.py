"""Tests for src.tools.ai.smart_evaluator — SMART evaluation + milestone alignment."""

import json
from unittest.mock import AsyncMock, patch, MagicMock

from src.tools.ai.smart_evaluator import (
    _evaluate_smart, _check_milestone_alignment,
    make_evaluate_smart, make_check_milestone_alignment,
)


def _mock_llm_response(content: str):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
    return response


class TestEvaluateSmart:
    @patch("src.tools.ai.smart_evaluator._client")
    async def test_success(self, mock_client):
        result_json = json.dumps({"overall_score": 8, "feedback": "Good issue"})
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_llm_response(result_json))

        result = await _evaluate_smart({"title": "Add auth", "body": "Implement OAuth"})
        assert result.success is True

    @patch("src.tools.ai.smart_evaluator._client")
    async def test_with_linked_issues(self, mock_client):
        result_json = json.dumps({"overall_score": 7})
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_llm_response(result_json))

        result = await _evaluate_smart(
            {"title": "Fix bug"}, linked_issues=[{"number": 1, "title": "Related"}]
        )
        assert result.success is True

    @patch("src.tools.ai.smart_evaluator._client")
    async def test_error(self, mock_client):
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("err"))
        result = await _evaluate_smart({"title": "test"})
        assert result.success is False


class TestCheckMilestoneAlignment:
    @patch("src.tools.ai.smart_evaluator._client")
    async def test_success(self, mock_client):
        result_json = json.dumps({"aligned": True, "confidence": 0.9})
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_llm_response(result_json))

        result = await _check_milestone_alignment(
            {"title": "Add login"}, {"title": "Auth milestone"}
        )
        assert result.success is True

    @patch("src.tools.ai.smart_evaluator._client")
    async def test_error(self, mock_client):
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("err"))
        result = await _check_milestone_alignment({}, {})
        assert result.success is False


class TestFactories:
    def test_make_evaluate_smart(self):
        assert make_evaluate_smart().name == "evaluate_smart"

    def test_make_check_milestone_alignment(self):
        assert make_check_milestone_alignment().name == "check_milestone_alignment"
