"""
AI tools for risk detection: secrets, security patterns, breaking changes, dependency issues.
"""

import json

import structlog
from src.core.config import settings
from src.core.llm_client import llm_json_call
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


async def _scan_secrets(diff: str) -> ToolResult:
    """Detect hardcoded secrets, API keys, tokens, passwords in a diff."""
    try:
        result = await llm_json_call(
            model=settings.ai_model_secret_scanner,
            messages=[
                {
                    "role": "system",
                    "content": """You are a security scanner. Scan this diff for hardcoded secrets.

Look for:
- API keys, tokens, passwords
- Private keys, certificates
- Database connection strings with credentials
- AWS/GCP/Azure credentials
- JWT secrets
- Webhook secrets
- Any string that looks like a credential (high entropy, known prefixes like sk-, ghp_, etc.)

IMPORTANT: Only flag things in ADDED lines (lines starting with +). Removed secrets are fine.

Respond with JSON:
{
  "secrets_found": [
    {
      "type": "api_key|password|token|private_key|connection_string|other",
      "file": "path if identifiable",
      "line_hint": "partial line content (redact the actual secret)",
      "severity": "critical|warning",
      "recommendation": "what to do"
    }
  ],
  "clean": true/false,
  "summary": "brief assessment"
}""",
                },
                {"role": "user", "content": diff[:40000]},
            ],
            caller="scan_secrets",
            temperature=0.1,
        )
        if result is None:
            return ToolResult(success=False, error="Secret scan failed after retries")
        return ToolResult(success=True, data=result)
    except Exception as e:
        log.warning("scan_secrets_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _scan_security_patterns(diff: str) -> ToolResult:
    """Detect SQL injection, XSS, unsafe deserialization, and other security patterns."""
    try:
        result = await llm_json_call(
            model=settings.ai_model_security_scanner,
            messages=[
                {
                    "role": "system",
                    "content": """You are a security pattern analyzer. Scan this diff for common vulnerability patterns.

Check for:
- SQL injection (string concatenation in queries, unsanitized input)
- XSS (unescaped user input in HTML/templates)
- Command injection (subprocess with user input, eval/exec)
- Path traversal (user input in file paths)
- Unsafe deserialization (pickle, yaml.load without SafeLoader)
- SSRF (user-controlled URLs in HTTP requests)
- Insecure crypto (MD5/SHA1 for security, weak random)
- Missing authentication/authorization checks

Only flag patterns in ADDED code. Be conservative — don't flag safe usage patterns.

Respond with JSON:
{
  "vulnerabilities": [
    {
      "type": "sql_injection|xss|command_injection|path_traversal|deserialization|ssrf|weak_crypto|auth_bypass|other",
      "severity": "critical|warning|info",
      "file": "path if identifiable",
      "description": "what was found",
      "recommendation": "how to fix"
    }
  ],
  "clean": true/false,
  "summary": "brief assessment"
}""",
                },
                {"role": "user", "content": diff[:40000]},
            ],
            caller="scan_security_patterns",
            temperature=0.1,
        )
        if result is None:
            return ToolResult(success=False, error="Security scan failed after retries")
        return ToolResult(success=True, data=result)
    except Exception as e:
        log.warning("scan_security_patterns_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _detect_breaking_changes(diff: str, files_changed: list[dict]) -> ToolResult:
    """Detect breaking API changes, schema changes, config changes."""
    try:
        result = await llm_json_call(
            model=settings.ai_model_breaking_changes,
            messages=[
                {
                    "role": "system",
                    "content": """You are a breaking change detector. Analyze this diff for changes that could break existing functionality.

Check for:
- API endpoint changes (renamed, removed, changed parameters)
- Database schema changes (column renames, drops, type changes)
- Configuration format changes
- Public interface changes (renamed functions, changed signatures)
- Removed or renamed exports
- Environment variable changes
- Protocol/format changes

Respond with JSON:
{
  "breaking_changes": [
    {
      "type": "api|schema|config|interface|export|env|protocol",
      "severity": "critical|warning|info",
      "description": "what changed",
      "impact": "who/what is affected",
      "migration_needed": true/false,
      "recommendation": "what to do"
    }
  ],
  "has_breaking_changes": true/false,
  "summary": "brief assessment"
}""",
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "files_changed": [f["filename"] for f in files_changed],
                        "diff": diff[:40000],
                    }),
                },
            ],
            caller="detect_breaking_changes",
        )
        if result is None:
            return ToolResult(success=False, error="Breaking change detection failed after retries")
        return ToolResult(success=True, data=result)
    except Exception as e:
        log.warning("detect_breaking_changes_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _check_dependency_changes(diff: str) -> ToolResult:
    """Parse lockfile/manifest diffs for new or changed dependencies."""
    try:
        result = await llm_json_call(
            model=settings.ai_model_dependency_checker,
            messages=[
                {
                    "role": "system",
                    "content": """You are a dependency analyst. Analyze this diff for dependency changes in package manifests or lockfiles.

Look at: package.json, pyproject.toml, requirements.txt, Cargo.toml, go.mod, Gemfile, pom.xml, etc.

Respond with JSON:
{
  "changes": [
    {
      "package": "package name",
      "action": "added|removed|upgraded|downgraded",
      "from_version": "old version or null",
      "to_version": "new version or null",
      "risk": "low|medium|high",
      "note": "any concern (major version bump, known issues, etc.)"
    }
  ],
  "has_dependency_changes": true/false,
  "summary": "brief assessment"
}""",
                },
                {"role": "user", "content": diff[:30000]},
            ],
            caller="check_dependency_changes",
        )
        if result is None:
            return ToolResult(success=False, error="Dependency check failed after retries")
        return ToolResult(success=True, data=result)
    except Exception as e:
        log.warning("check_dependency_changes_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_scan_secrets() -> Tool:
    return Tool(
        name="scan_secrets",
        description="AI tool: Scan a diff for hardcoded secrets, API keys, tokens, and passwords. Only checks added lines.",
        parameters={
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "The diff content to scan"},
            },
            "required": ["diff"],
        },
        handler=lambda diff: _scan_secrets(diff),
    )


def make_scan_security_patterns() -> Tool:
    return Tool(
        name="scan_security_patterns",
        description="AI tool: Scan a diff for security vulnerability patterns (SQL injection, XSS, command injection, etc.).",
        parameters={
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "The diff content to scan"},
            },
            "required": ["diff"],
        },
        handler=lambda diff: _scan_security_patterns(diff),
    )


def make_detect_breaking_changes() -> Tool:
    return Tool(
        name="detect_breaking_changes",
        description="AI tool: Detect breaking API changes, schema changes, config changes in a diff.",
        parameters={
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "The diff content"},
                "files_changed": {"type": "array", "items": {"type": "object"}, "description": "List of changed files"},
            },
            "required": ["diff", "files_changed"],
        },
        handler=lambda diff, files_changed: _detect_breaking_changes(diff, files_changed),
    )


def make_check_dependency_changes() -> Tool:
    return Tool(
        name="check_dependency_changes",
        description="AI tool: Analyze dependency changes in package manifests or lockfiles for risk.",
        parameters={
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "The diff content (ideally just the manifest/lockfile portion)"},
            },
            "required": ["diff"],
        },
        handler=lambda diff: _check_dependency_changes(diff),
    )
