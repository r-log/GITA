"""ORM model for graph nodes — symbols as first-class queryable entities."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, DateTime, ForeignKey, UniqueConstraint, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class GraphNode(Base):
    __tablename__ = "graph_nodes"
    __table_args__ = (
        UniqueConstraint("repo_id", "qualified_name", name="uq_graph_node_repo_qname"),
        Index("ix_graph_node_repo_type", "repo_id", "node_type"),
        Index("ix_graph_node_repo_file", "repo_id", "file_path"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    node_type: Mapped[str] = mapped_column(
        String(20), nullable=False
    )  # file, module, class, function, method, route, constant
    qualified_name: Mapped[str] = mapped_column(
        String(500), nullable=False
    )  # e.g. "src/models/issue.py::IssueModel"
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)  # short name
    language: Mapped[str] = mapped_column(String(30), nullable=False)
    line_number: Mapped[Optional[int]] = mapped_column(Integer)
    extra: Mapped[Optional[dict]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    repository = relationship("Repository")
