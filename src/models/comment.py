"""ORM model for issue/PR comments."""

from datetime import datetime
from typing import Optional
from sqlalchemy import BigInteger, Integer, String, Boolean, Text, DateTime, ForeignKey, Index, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class CommentModel(Base):
    __tablename__ = "comments"
    __table_args__ = (
        Index("ix_comment_repo_target", "repo_id", "target_type", "target_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    target_type: Mapped[str] = mapped_column(String(10), nullable=False)  # "issue" or "pr"
    target_number: Mapped[int] = mapped_column(Integer, nullable=False)
    author_login: Mapped[Optional[str]] = mapped_column(String(100))
    body: Mapped[Optional[str]] = mapped_column(Text)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    github_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    github_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    repository = relationship("Repository")
