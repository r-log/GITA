"""
ORM model for outcome tracking — the foundational learning layer.

Every measurable agent action schedules an outcome row. 24-72h later,
a worker checks what the agent predicted against what actually happened
and records the verdict. This is how GITA learns whether its interventions
are working.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import Integer, String, Text, DateTime, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class OutcomeType(str, Enum):
    """Types of measurable outcomes. Stored as VARCHAR to keep migrations cheap."""
    SMART_EVAL = "smart_eval"
    CLOSURE_VALIDATION = "closure_validation"
    CHECKLIST_CORRECTION = "checklist_correction"
    RISK_WARNING = "risk_warning"
    STALE_NUDGE = "stale_nudge"
    DEADLINE_PREDICTION = "deadline_prediction"


class OutcomeStatus(str, Enum):
    """Lifecycle states of an outcome row."""
    PENDING = "pending"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    STALE = "stale"


class OutcomeModel(Base):
    __tablename__ = "outcomes"
    __table_args__ = (
        # Fast poller query: WHERE status='pending' AND scheduled_for <= now()
        Index("ix_outcomes_pending_due", "status", "scheduled_for"),
        # Fast target history query: WHERE repo_id=? AND target_type=? AND target_number=?
        Index("ix_outcomes_target", "repo_id", "target_type", "target_number"),
        # Structural dedup: one outcome per (agent_run, outcome_type)
        UniqueConstraint("agent_run_id", "outcome_type", name="uq_outcomes_run_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"), index=True)
    agent_run_id: Mapped[int] = mapped_column(ForeignKey("agent_runs.id"), index=True)

    outcome_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)  # issue, pr, milestone
    target_number: Mapped[Optional[int]] = mapped_column(Integer)

    predicted: Mapped[dict] = mapped_column(JSONB, default=dict)
    observed: Mapped[Optional[dict]] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(String(16), default=OutcomeStatus.PENDING.value, nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    notes: Mapped[Optional[str]] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    repository = relationship("Repository")
