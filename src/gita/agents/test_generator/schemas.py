"""Pydantic models for LLM I/O in the test-generator agent.

One response schema — the LLM produces the full test file content plus
a self-report of what it covered and its confidence.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class GeneratedTestResponse(BaseModel):
    """LLM output for the test-generation call."""

    test_file_content: str = Field(
        description=(
            "Complete pytest test file, including all necessary imports. "
            "Must be self-contained — no reliance on fixtures that aren't "
            "defined in this file or available via standard pytest."
        )
    )
    covered_symbols: list[str] = Field(
        default_factory=list,
        description=(
            "Public names from the target module that this test file "
            "exercises (functions, classes, methods). Used for audit, not "
            "re-verified."
        ),
    )
    notes: str = Field(
        default="",
        description=(
            "Caveats, scenarios skipped, or context a reviewer would "
            "want to know. Kept short."
        ),
    )
    confidence: float = Field(
        default=0.0,
        description=(
            "Self-assessed confidence in the generated tests (0.0 - 1.0). "
            "Blended with verification pass/fail before the Decision "
            "framework sees it."
        ),
    )
