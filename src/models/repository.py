"""ORM model for tracked GitHub repositories."""

from datetime import datetime
from sqlalchemy import BigInteger, String, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(primary_key=True)
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)  # "owner/repo"
    installation_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # Relationships
    milestones = relationship("MilestoneModel", back_populates="repository")
    issues = relationship("IssueModel", back_populates="repository")
    pull_requests = relationship("PullRequestModel", back_populates="repository")
    analyses = relationship("Analysis", back_populates="repository")
    agent_runs = relationship("AgentRun", back_populates="repository")
