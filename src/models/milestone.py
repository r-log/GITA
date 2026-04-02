"""ORM model for milestone tracking."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Float, Boolean, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class MilestoneModel(Base):
    __tablename__ = "milestones"
    __table_args__ = (UniqueConstraint("repo_id", "github_number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    github_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(255))
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime)
    state: Mapped[Optional[str]] = mapped_column(String(20))
    total_issues: Mapped[int] = mapped_column(Integer, default=0)
    closed_issues: Mapped[int] = mapped_column(Integer, default=0)
    completion_pct: Mapped[float] = mapped_column(Float, default=0.0)
    velocity_trend: Mapped[Optional[float]] = mapped_column(Float)
    on_track: Mapped[Optional[bool]] = mapped_column(Boolean)
    last_analyzed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    repository = relationship("Repository", back_populates="milestones")
    issues = relationship("IssueModel", back_populates="milestone")
