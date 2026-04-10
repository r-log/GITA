"""
AI tools for code analysis: diff quality, test coverage checking.
"""

import json
import structlog
from src.core.config import settings
from src.core.llm_client import llm_json_call
from src.tools.base import Tool, ToolResult

log = structlog.get_logger()


async def _analyze_diff_quality(diff: str, pr_info: dict) -> ToolResult:
    """Analyze a PR diff for code quality issues."""
    try:
        result = await llm_json_call(
            model=settings.ai_model_diff_analyzer,
            messages=[
                {
                    "role": "system",
                    "content": """You are a senior code reviewer. Analyze this pull request diff for quality issues.

Check for:
- Code clarity and readability
- Error handling gaps
- Potential bugs or logic errors
- Code duplication
- Naming conventions
- Large functions that should be split
- Missing input validation at system boundaries

Do NOT flag:
- Style preferences (formatting is handled by linters)
- Minor nitpicks
- Things that are clearly intentional

Respond with JSON:
{
  "overall_quality": "good|acceptable|needs_work|poor",
  "score": 0.0-1.0,
  "issues": [
    {
      "severity": "info|warning|error",
      "file": "path/to/file",
      "description": "what's wrong",
      "suggestion": "how to fix it"
    }
  ],
  "positives": ["things done well"],
  "summary": "one-paragraph summary"
}""",
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "pr": {
                            "title": pr_info.get("title"),
                            "body": pr_info.get("body", "")[:2000],
                            "files_changed": pr_info.get("files_changed"),
                        },
                        "diff": diff[:40000],
                    }),
                },
            ],
            caller="analyze_diff_quality",
        )
        if result is None:
            return ToolResult(success=False, error="Diff analysis failed after retries")
        return ToolResult(success=True, data=result)
    except Exception as e:
        log.warning("code_analyzer_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


async def _check_test_coverage(diff: str, files_changed: list[dict]) -> ToolResult:
    """Check if new code paths in the diff have corresponding tests."""
    try:
        test_files = [f for f in files_changed if "test" in f.get("filename", "").lower()]
        source_files = [f for f in files_changed if "test" not in f.get("filename", "").lower()]

        result = await llm_json_call(
            model=settings.ai_model_test_coverage,
            messages=[
                {
                    "role": "system",
                    "content": """You are a test coverage analyst. Given a PR diff and changed file list, determine if new code paths are tested.

Respond with JSON:
{
  "has_tests": true/false,
  "coverage_assessment": "good|partial|missing|not_applicable",
  "test_files_changed": ["list of test files in the PR"],
  "source_files_without_tests": ["source files that add logic but have no corresponding test changes"],
  "suggestions": ["specific suggestions for what to test"],
  "summary": "brief assessment"
}""",
                },
                {
                    "role": "user",
                    "content": json.dumps({
                        "source_files": [f["filename"] for f in source_files],
                        "test_files": [f["filename"] for f in test_files],
                        "diff": diff[:30000],
                    }),
                },
            ],
            caller="check_test_coverage",
        )
        if result is None:
            return ToolResult(success=False, error="Test coverage check failed after retries")
        return ToolResult(success=True, data=result)
    except Exception as e:
        log.warning("code_analyzer_failed", error=str(e), exc_info=True)
        return ToolResult(success=False, error=str(e))


def make_analyze_diff_quality() -> Tool:
    return Tool(
        name="analyze_diff_quality",
        description="AI tool: Analyze a PR diff for code quality issues. Returns quality score, issues found, and suggestions.",
        parameters={
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "The PR diff content"},
                "pr_info": {"type": "object", "description": "PR metadata (title, body, files_changed count)"},
            },
            "required": ["diff", "pr_info"],
        },
        handler=lambda diff, pr_info: _analyze_diff_quality(diff, pr_info),
    )


def make_check_test_coverage() -> Tool:
    return Tool(
        name="check_test_coverage",
        description="AI tool: Check if new code paths in the diff have corresponding test changes.",
        parameters={
            "type": "object",
            "properties": {
                "diff": {"type": "string", "description": "The PR diff content"},
                "files_changed": {"type": "array", "items": {"type": "object"}, "description": "List of changed files with filename and status"},
            },
            "required": ["diff", "files_changed"],
        },
        handler=lambda diff, files_changed: _check_test_coverage(diff, files_changed),
    )
