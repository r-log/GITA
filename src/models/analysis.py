"""ORM model for general analysis log (PRs + issues + milestones)."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Float, Boolean, BigInteger, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class Analysis(Base):
    __tablename__ = "analyses"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    target_type: Mapped[str] = mapped_column(String(20), nullable=False)  # 'issue', 'pr', 'milestone'
    target_number: Mapped[int] = mapped_column(Integer, nullable=False)
    analysis_type: Mapped[Optional[str]] = mapped_column(String(50))  # 'smart', 'risk', 'quality', 'progress'
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    score: Mapped[Optional[float]] = mapped_column(Float)
    risk_level: Mapped[Optional[str]] = mapped_column(String(20))
    comment_posted: Mapped[bool] = mapped_column(Boolean, default=False)
    comment_id: Mapped[Optional[int]] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Relationships
    repository = relationship("Repository", back_populates="analyses")
