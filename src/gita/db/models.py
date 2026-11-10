import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover
    Vector = None  # type: ignore[assignment,misc]


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repos"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    # Unique case-insensitively (see __table_args__): lookups lower() it.
    github_full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Per-repo opt-in for the post-reindex auto-test-generation trigger.
    # Defaults to False so no existing repo gets surprise PRs;
    # flipped on via ``gita index --auto-test-gen``.
    auto_test_generation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Remote default branch (e.g. "main", "master", "develop"). Only pushes
    # to it reindex, and auto-generated test PRs target it.
    # Discovered via ``discover_default_branch`` at index time;
    # overridable via ``gita index --default-branch BRANCH``.
    default_branch: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default="main", default="main"
    )
    # History memory (gita.history): the last first-parent commit synced.
    history_synced_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    history_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    files: Mapped[list["CodeIndex"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )
    edges: Mapped[list["ImportEdge"]] = relationship(
        back_populates="repo", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index(
            "ix_repos_github_full_name_lower",
            func.lower(github_full_name),
            unique=True,
            postgresql_where=github_full_name.isnot(None),
        ),
    )


class CodeIndex(Base):
    __tablename__ = "code_index"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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
    embedding = mapped_column(
        Vector(1536) if Vector is not None else Text,
        nullable=True,
    )

    repo: Mapped[Repo] = relationship(back_populates="files")

    __table_args__ = (
        UniqueConstraint("repo_id", "file_path", name="uq_code_index_repo_file"),
        Index("ix_code_index_repo_id", "repo_id"),
        Index("ix_code_index_language", "language"),
        # Full-text search for concept_view ('simple': no stemming, which
        # suits identifiers). Queries must use the same expression.
        Index(
            "ix_code_index_content_fts",
            text("to_tsvector('simple', COALESCE(content, ''))"),
            postgresql_using="gin",
        ),
        # Cosine-similarity search over file embeddings.
        Index(
            "ix_code_index_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )


class ImportEdge(Base):
    __tablename__ = "import_edges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
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


class AgentAction(Base):
    """Persistent record of every decision that passes the write gate.

    Primary purpose is idempotency: before executing a Decision, the
    framework computes its identity (``signature``) and reserves
    ``(repo_name, agent, action, signature, live)``. An existing row
    short-circuits the decision with ``Outcome.DEDUPED``; see
    ``gita.agents.dedupe``.

    Secondary purpose is audit trail — ``evidence`` carries the reasoning
    chain the agent produced, and ``external_id`` holds the GitHub ID of
    the resulting issue/comment so we can trace any artifact back to the
    agent run that created it.
    """

    __tablename__ = "agent_actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    signature: Mapped[str] = mapped_column(String(64), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # False for shadow-mode observations (nothing reached GitHub). Dedupe
    # checks never let a shadow row suppress a live side effect.
    live: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true", default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "repo_name",
            "agent",
            "action",
            "signature",
            "live",
            name="uq_agent_actions_signature",
        ),
        Index("ix_agent_actions_repo_name", "repo_name"),
    )


# ---------------------------------------------------------------------------
# History memory (gita.history): merged PRs and the defects later fixed in them
# ---------------------------------------------------------------------------
class HistoryPR(Base):
    """A merged pull request, read from the default branch's history."""

    __tablename__ = "history_prs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str | None] = mapped_column(Text, nullable=True)
    # fix / feature / refactor / docs / test / chore / other
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    merge_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    merged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    author: Mapped[str] = mapped_column(Text, nullable=False)
    files_changed: Mapped[int] = mapped_column(Integer, nullable=False)
    additions: Mapped[int] = mapped_column(Integer, nullable=False)
    deletions: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("repo_id", "number", name="uq_history_prs_repo_number"),
        Index("ix_history_prs_repo_merged_at", "repo_id", "merged_at"),
    )


class HistoryPRFile(Base):
    """A file a merged PR changed (``old_path`` when git saw a rename)."""

    __tablename__ = "history_pr_files"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    old_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    additions: Mapped[int] = mapped_column(Integer, nullable=False)
    deletions: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_history_pr_files_repo_path", "repo_id", "path"),
        Index("ix_history_pr_files_repo_pr", "repo_id", "pr_number"),
    )


class HistoryPRCommit(Base):
    """Which merged PR brought a commit in (the first one that did)."""

    __tablename__ = "history_pr_commits"

    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True
    )
    commit_sha: Mapped[str] = mapped_column(String(64), primary_key=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False)


class DefectLink(Base):
    """A fix PR changed lines another PR wrote: evidence that PR introduced a defect.

    ``share`` is that PR's part of the fix's blamed product lines; ``lines``
    are where they sat in its head. ``introducing_pr`` is NULL for code
    older than every tracked PR.
    """

    __tablename__ = "defect_links"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    repo_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("repos.id", ondelete="CASCADE"), nullable=False
    )
    fix_pr: Mapped[int] = mapped_column(Integer, nullable=False)
    introducing_pr: Mapped[int | None] = mapped_column(Integer, nullable=True)
    share: Mapped[float] = mapped_column(Float, nullable=False)
    lines: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)

    __table_args__ = (
        Index("ix_defect_links_repo_fix", "repo_id", "fix_pr"),
        Index("ix_defect_links_repo_introducing", "repo_id", "introducing_pr"),
    )
