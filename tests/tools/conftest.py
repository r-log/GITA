"""Tool-specific test fixtures."""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def patch_github_client():
    """
    Context-manager fixture that patches GitHubClient in a given module path.

    Usage:
        with patch_github_client("src.tools.github.issues") as mock_cls:
            mock_cls.return_value = mock_github_client(...)
    """
    def _patch(module_path: str):
        return patch(f"{module_path}.GitHubClient")
    return _patch


@pytest.fixture
def patch_openai_client():
    """
    Patches the module-level _client in an AI tool module.

    Usage:
        with patch_openai_client("src.tools.ai.risk_scanner") as mock_client:
            mock_client.chat.completions.create = AsyncMock(return_value=...)
    """
    def _patch(module_path: str):
        return patch(f"{module_path}._client")
    return _patch
