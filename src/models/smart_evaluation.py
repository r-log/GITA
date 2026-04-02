"""ORM model for S.M.A.R.T. evaluation history."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, Float, Boolean, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class SmartEvaluationModel(Base):
    __tablename__ = "smart_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    issue_id: Mapped[int] = mapped_column(ForeignKey("issues.id"))
    is_milestone: Mapped[bool] = mapped_column(Boolean, default=False)
    overall_score: Mapped[Optional[float]] = mapped_column(Float)
    specific_score: Mapped[Optional[float]] = mapped_column(Float)
    measurable_score: Mapped[Optional[float]] = mapped_column(Float)
    achievable_score: Mapped[Optional[float]] = mapped_column(Float)
    relevant_score: Mapped[Optional[float]] = mapped_column(Float)
    time_bound_score: Mapped[Optional[float]] = mapped_column(Float)
    findings: Mapped[dict] = mapped_column(JSONB, default=dict)
    suggestions: Mapped[dict] = mapped_column(JSONB, default=dict)
    action_items: Mapped[dict] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    issue = relationship("IssueModel", back_populates="smart_evaluations")
