"""ORM model for graph edges — relationships between nodes and project entities."""

from datetime import datetime
from typing import Optional
from sqlalchemy import Integer, String, Float, DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


class GraphEdge(Base):
    __tablename__ = "graph_edges"
    __table_args__ = (
        Index("ix_graph_edge_repo_type", "repo_id", "edge_type"),
        Index("ix_graph_edge_source", "source_node_id"),
        Index("ix_graph_edge_target", "target_node_id"),
        Index("ix_graph_edge_entity", "repo_id", "target_entity_type", "target_entity_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repositories.id"))
    source_node_id: Mapped[int] = mapped_column(ForeignKey("graph_nodes.id", ondelete="CASCADE"))
    target_node_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("graph_nodes.id", ondelete="CASCADE")
    )  # null when target is an external entity (issue/milestone/pr)
    edge_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # imports, defines, calls, inherits, belongs_to_milestone, belongs_to_issue, changed_in_pr
    target_entity_type: Mapped[Optional[str]] = mapped_column(
        String(20)
    )  # issue, milestone, pr — used when target_node_id is null
    target_entity_id: Mapped[Optional[int]] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    extra: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    repository = relationship("Repository")
    source_node = relationship("GraphNode", foreign_keys=[source_node_id])
    target_node = relationship("GraphNode", foreign_keys=[target_node_id])
