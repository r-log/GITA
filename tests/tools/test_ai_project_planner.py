"""Tests for src.tools.ai.project_planner — LLM-based planning + fuzzy matching."""

import json
from unittest.mock import AsyncMock, patch, MagicMock

from src.tools.ai.project_planner import (
    _infer_project_plan, _compare_plan_vs_state, _fuzzy_match_milestone,
    make_infer_project_plan, make_compare_plan_vs_state, make_fuzzy_match_milestone,
)


def _mock_llm_response(content: str):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
    return response


class TestInferProjectPlan:
    @patch("src.tools.ai.project_planner._client")
    async def test_success(self, mock_client):
        result_json = json.dumps({"project_summary": "A web app", "milestones": [{"title": "v1"}]})
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_llm_response(result_json))

        result = await _infer_project_plan("A web app with auth")
        assert result.success is True
        assert "milestones" in result.data

    @patch("src.tools.ai.project_planner._client")
    async def test_error(self, mock_client):
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("timeout"))

        result = await _infer_project_plan("desc")
        assert result.success is False


class TestComparePlanVsState:
    @patch("src.tools.ai.project_planner._client")
    async def test_success(self, mock_client):
        result_json = json.dumps({"actions": [], "summary": "No changes needed"})
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_llm_response(result_json))

        result = await _compare_plan_vs_state("plan", "state")
        assert result.success is True

    @patch("src.tools.ai.project_planner._client")
    async def test_error(self, mock_client):
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("err"))

        result = await _compare_plan_vs_state("plan", "state")
        assert result.success is False


class TestFuzzyMatchMilestone:
    async def test_high_match(self):
        milestones = [{"title": "Authentication System", "number": 1}]
        result = await _fuzzy_match_milestone("Authentication System", milestones)
        assert result.success is True
        assert result.data["match"] is not None
        assert result.data["score"] >= 80

    async def test_no_match(self):
        milestones = [{"title": "Completely Different", "number": 1}]
        result = await _fuzzy_match_milestone("Authentication System", milestones)
        assert result.success is True

    async def test_empty_milestones(self):
        result = await _fuzzy_match_milestone("Test", [])
        assert result.success is True


class TestFactories:
    def test_make_infer_project_plan(self):
        assert make_infer_project_plan().name == "infer_project_plan"

    def test_make_compare_plan_vs_state(self):
        assert make_compare_plan_vs_state().name == "compare_plan_vs_state"

    def test_make_fuzzy_match_milestone(self):
        assert make_fuzzy_match_milestone().name == "fuzzy_match_milestone"
