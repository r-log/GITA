"""ORM model for tracking every agent invocation (audit + debugging)."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Float, Text, DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class AgentRun(Base):
    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    agent_name: Mapped[str] = mapped_column(String(50), nullable=False)
    event_type: Mapped[Optional[str]] = mapped_column(String(100))
    context: Mapped[dict] = mapped_column(JSONB, nullable=False)  # AgentContext snapshot
    tools_called: Mapped[dict] = mapped_column(JSONB, default=list)  # ordered tool calls + results
    result: Mapped[Optional[dict]] = mapped_column(JSONB)  # AgentResult snapshot
    status: Mapped[str] = mapped_column(String(20), nullable=False)  # running, success, partial, failed
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    error_message: Mapped[Optional[str]] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    # Relationships
    repository = relationship("Repository", back_populates="agent_runs")
