"""ORM model for onboarding run records."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Float, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class OnboardingRun(Base):
    __tablename__ = "onboarding_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # running, success, partial, failed
    repo_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)  # tree + key file contents
    suggested_plan: Mapped[dict] = mapped_column(JSONB, default=dict)  # AI-inferred milestones/tasks
    existing_state: Mapped[dict] = mapped_column(JSONB, default=dict)  # milestones/issues that existed
    actions_taken: Mapped[dict] = mapped_column(JSONB, default=list)  # create/update/skip/flag actions
    milestones_created: Mapped[int] = mapped_column(Integer, default=0)
    milestones_updated: Mapped[int] = mapped_column(Integer, default=0)
    issues_created: Mapped[int] = mapped_column(Integer, default=0)
    issues_updated: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    repository = relationship("Repository")
