"""
DB/GitHub tools for per-repo configuration via .github/assistant.yml.
"""

import yaml
from src.core.github_auth import GitHubClient
from src.tools.base import Tool, ToolResult

# Default config values
DEFAULT_CONFIG = {
    "agents": {
        "onboarding": {
            "auto_create_milestones": True,
            "confidence_threshold": 0.7,
        },
        "issue_analyst": {
            "smart_threshold": 0.7,
            "comment_on_new_issues": True,
        },
        "pr_reviewer": {
            "max_diff_lines": 500,
            "require_linked_issue": True,
        },
        "risk_detective": {
            "security_scan": True,
            "breaking_change_detection": True,
            "block_on_critical": True,
        },
        "progress_tracker": {
            "stale_days": 14,
            "tag_assignees": True,
            "milestone_reminders": True,
        },
    },
    "supervisor": {
        "max_parallel_agents": 3,
        "comment_cooldown_minutes": 60,
    },
}


async def _get_repo_config(installation_id: int, repo_full_name: str) -> ToolResult:
    """
    Fetch .github/assistant.yml from the repo and merge with defaults.
    If the file doesn't exist, returns defaults.
    """
    client = GitHubClient(installation_id)
    try:
        import base64
        data = await client.get(
            f"/repos/{repo_full_name}/contents/.github/assistant.yml",
        )
        content = base64.b64decode(data["content"]).decode("utf-8")
        repo_config = yaml.safe_load(content) or {}

        # Deep merge with defaults
        merged = _deep_merge(DEFAULT_CONFIG, repo_config)
        return ToolResult(success=True, data=merged)
    except Exception:
        # File doesn't exist or can't be parsed — use defaults
        return ToolResult(success=True, data=DEFAULT_CONFIG)


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def make_get_repo_config(installation_id: int, repo_full_name: str) -> Tool:
    return Tool(
        name="get_repo_config",
        description="Load per-repo configuration from .github/assistant.yml, merged with defaults.",
        parameters={"type": "object", "properties": {}, "required": []},
        handler=lambda: _get_repo_config(installation_id, repo_full_name),
    )
