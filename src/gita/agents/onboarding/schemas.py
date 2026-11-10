"""Pydantic models for LLM I/O validation in the onboarding agent.

Each model maps to one of the three structured-output calls in the
onboarding recipe. They're kept separate from the recipe so tests can
import them without pulling in the full agent pipeline.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["low", "medium", "high", "critical"]
FindingKind = Literal["bug", "security", "quality", "missing", "design"]


class PickFilesResponse(BaseModel):
    project_summary: str = Field(description="2-3 sentence description of what the project is")
    tech_stack: list[str] = Field(
        default_factory=list,
        description="Language, framework, notable libraries",
    )
    picks: list[int] = Field(description="0-based indices into the load_bearing list")
    reasoning: str = ""


class LLMFinding(BaseModel):
    """One finding as the model reports it.

    ``severity`` and ``kind`` are closed vocabularies: the JSON schema
    sent to the provider lists them as enums, and anything else fails
    validation instead of flowing into rendered output and verdict
    logic. Case and surrounding whitespace are forgiven.
    """

    model_config = ConfigDict(extra="forbid")

    file: str
    line: int
    severity: Severity
    kind: FindingKind
    description: str
    fix_sketch: str = ""

    @field_validator("severity", "kind", mode="before")
    @classmethod
    def _normalize_vocab(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value


class FindingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[LLMFinding] = Field(default_factory=list)


class LLMMilestone(BaseModel):
    title: str
    summary: str
    finding_indices: list[int] = Field(default_factory=list)
    confidence: float = 0.0


class MilestonesResponse(BaseModel):
    milestones: list[LLMMilestone] = Field(default_factory=list)
