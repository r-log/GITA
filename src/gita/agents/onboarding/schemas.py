"""Pydantic models for LLM I/O validation in the onboarding agent.

Each model maps to one of the three structured-output calls in the
onboarding recipe. They're kept separate from the recipe so tests can
import them without pulling in the full agent pipeline.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class PickFilesResponse(BaseModel):
    project_summary: str = Field(
        description="2-3 sentence description of what the project is"
    )
    tech_stack: list[str] = Field(
        default_factory=list,
        description="Language, framework, notable libraries",
    )
    picks: list[int] = Field(
        description="0-based indices into the load_bearing list"
    )
    reasoning: str = ""


class LLMFinding(BaseModel):
    file: str
    line: int
    severity: str
    kind: str
    description: str
    fix_sketch: str = ""


class FindingsResponse(BaseModel):
    findings: list[LLMFinding] = Field(default_factory=list)


class LLMMilestone(BaseModel):
    title: str
    summary: str
    finding_indices: list[int] = Field(default_factory=list)
    confidence: float = 0.0


class MilestonesResponse(BaseModel):
    milestones: list[LLMMilestone] = Field(default_factory=list)
