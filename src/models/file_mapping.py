"""ORM model for file-to-issue mapping (enables drift detection on push)."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Float, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class FileMapping(Base):
    __tablename__ = "file_mappings"
    __table_args__ = (UniqueConstraint("repo_id", "file_path"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    milestone_id: Mapped[Optional[int]] = mapped_column(ForeignKey("milestones.id"))
    issue_id: Mapped[Optional[int]] = mapped_column(ForeignKey("issues.id"))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    repository = relationship("Repository")
    milestone = relationship("MilestoneModel")
    issue = relationship("IssueModel")
