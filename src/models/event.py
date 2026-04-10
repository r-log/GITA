"""ORM model for raw webhook events — the universal activity stream."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class EventModel(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_event_repo_type", "repo_id", "event_type"),
        Index("ix_event_repo_target", "repo_id", "target_type", "target_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    delivery_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    action: Mapped[Optional[str]] = mapped_column(String(50))
    sender_login: Mapped[Optional[str]] = mapped_column(String(100))
    target_type: Mapped[Optional[str]] = mapped_column(String(20))  # "issue", "pr", "push"
    target_number: Mapped[Optional[int]] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    repository = relationship("Repository")
