import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    files: Mapped[list["CodeIndex"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )
    edges: Mapped[list["ImportEdge"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )


class CodeIndex(Base):
    __tablename__ = "code_index"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repos.id", ondelete="CASCADE"),
        nullable=False,
    )
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    indexed_at_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    structure: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    repo: Mapped[Repo] = relationship(back_populates="files")

    __table_args__ = (
        UniqueConstraint("repo_id", "file_path", name="uq_code_index_repo_file"),
        Index("ix_code_index_repo_id", "repo_id"),
        Index("ix_code_index_language", "language"),
    )


class ImportEdge(Base):
    __tablename__ = "import_edges"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("repos.id", ondelete="CASCADE"),
        nullable=False,
    )
    src_file: Mapped[str] = mapped_column(Text, nullable=False)
    dst_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_import: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(32), nullable=False)

    repo: Mapped[Repo] = relationship(back_populates="edges")

    __table_args__ = (
        Index("ix_import_edges_repo_src", "repo_id", "src_file"),
        Index("ix_import_edges_repo_dst", "repo_id", "dst_file"),
    )
