"""Tests for src.tools.db.config — _deep_merge and config loading."""

import pytest
from unittest.mock import AsyncMock, patch

from src.tools.db.config import _deep_merge, _get_repo_config, DEFAULT_CONFIG, make_get_repo_config


class TestDeepMerge:
    def test_flat_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3}

    def test_nested_override(self):
        base = {"agents": {"onboarding": {"auto": True, "threshold": 0.5}}}
        override = {"agents": {"onboarding": {"threshold": 0.8}}}
        result = _deep_merge(base, override)
        assert result["agents"]["onboarding"]["auto"] is True
        assert result["agents"]["onboarding"]["threshold"] == 0.8

    def test_new_keys_added(self):
        base = {"a": 1}
        override = {"b": 2}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 2}

    def test_does_not_mutate_base(self):
        base = {"a": {"b": 1}}
        override = {"a": {"b": 2}}
        result = _deep_merge(base, override)
        assert base["a"]["b"] == 1  # Base unchanged
        assert result["a"]["b"] == 2

    def test_empty_override(self):
        base = {"a": 1, "b": 2}
        result = _deep_merge(base, {})
        assert result == {"a": 1, "b": 2}


class TestDefaultConfig:
    def test_has_all_agent_sections(self):
        assert "agents" in DEFAULT_CONFIG
        agents = DEFAULT_CONFIG["agents"]
        assert "onboarding" in agents
        assert "issue_analyst" in agents
        assert "pr_reviewer" in agents
        assert "risk_detective" in agents
        assert "progress_tracker" in agents

    def test_has_supervisor_section(self):
        assert "supervisor" in DEFAULT_CONFIG


class TestGetRepoConfig:
    @patch("src.tools.db.config.GitHubClient")
    async def test_fallback_to_defaults_on_error(self, mock_cls):
        mock_cls.return_value.get = AsyncMock(side_effect=Exception("Not found"))

        result = await _get_repo_config(1001, "owner/repo")
        assert result.success is True
        assert result.data == DEFAULT_CONFIG


class TestFactory:
    def test_make_get_repo_config(self):
        tool = make_get_repo_config(1001, "owner/repo")
        assert tool.name == "get_repo_config"
