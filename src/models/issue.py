"""ORM model for issue snapshots."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Boolean, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class IssueModel(Base):
    __tablename__ = "issues"
    __table_args__ = (UniqueConstraint("repo_id", "github_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    github_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    state: Mapped[Optional[str]] = mapped_column(String(20))
    milestone_id: Mapped[Optional[int]] = mapped_column(ForeignKey("milestones.id"))
    assignees: Mapped[dict] = mapped_column(JSONB, default=list)
    labels: Mapped[dict] = mapped_column(JSONB, default=list)
    is_milestone_tracker: Mapped[bool] = mapped_column(Boolean, default=False)
    linked_issue_numbers: Mapped[dict] = mapped_column(JSONB, default=list)
    last_activity_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    repository = relationship("Repository", back_populates="issues")
    milestone = relationship("MilestoneModel", back_populates="issues")
    smart_evaluations = relationship("SmartEvaluationModel", back_populates="issue")
