"""Tests for src.tools.ai.risk_scanner — security scanning tools."""

import json
from unittest.mock import AsyncMock, patch, MagicMock

from src.tools.ai.risk_scanner import (
    _scan_secrets, _scan_security_patterns,
    _detect_breaking_changes, _check_dependency_changes,
)


def _mock_llm_response(content: str):
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message.content = content
    response.usage = MagicMock(prompt_tokens=100, completion_tokens=50)
    return response


class TestScanSecrets:
    @patch("src.tools.ai.risk_scanner._client")
    async def test_success(self, mock_client):
        result_json = json.dumps({"secrets_found": [], "clean": True, "summary": "No secrets"})
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_llm_response(result_json))

        result = await _scan_secrets("+API_KEY = 'test'")
        assert result.success is True

    @patch("src.tools.ai.risk_scanner._client")
    async def test_error(self, mock_client):
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("err"))
        result = await _scan_secrets("diff")
        assert result.success is False


class TestScanSecurityPatterns:
    @patch("src.tools.ai.risk_scanner._client")
    async def test_success(self, mock_client):
        result_json = json.dumps({"vulnerabilities": [], "clean": True, "summary": "Clean"})
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_llm_response(result_json))

        result = await _scan_security_patterns("+sql = f'SELECT * FROM {table}'")
        assert result.success is True

    @patch("src.tools.ai.risk_scanner._client")
    async def test_error(self, mock_client):
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("err"))
        result = await _scan_security_patterns("diff")
        assert result.success is False


class TestDetectBreakingChanges:
    @patch("src.tools.ai.risk_scanner._client")
    async def test_success(self, mock_client):
        result_json = json.dumps({"breaking_changes": [], "has_breaking_changes": False, "summary": "None"})
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_llm_response(result_json))

        result = await _detect_breaking_changes("+new code", [{"filename": "api.py"}])
        assert result.success is True

    @patch("src.tools.ai.risk_scanner._client")
    async def test_error(self, mock_client):
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("err"))
        result = await _detect_breaking_changes("diff", [])
        assert result.success is False


class TestCheckDependencyChanges:
    @patch("src.tools.ai.risk_scanner._client")
    async def test_success(self, mock_client):
        result_json = json.dumps({"changes": [], "has_dependency_changes": False, "summary": "None"})
        mock_client.chat.completions.create = AsyncMock(return_value=_mock_llm_response(result_json))

        result = await _check_dependency_changes("+new-pkg: ^1.0")
        assert result.success is True

    @patch("src.tools.ai.risk_scanner._client")
    async def test_error(self, mock_client):
        mock_client.chat.completions.create = AsyncMock(side_effect=Exception("err"))
        result = await _check_dependency_changes("diff")
        assert result.success is False
