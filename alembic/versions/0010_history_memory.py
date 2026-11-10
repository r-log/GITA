"""History memory: merged PRs, their files and commits, and defect links

Tables for gita.history: every merged PR on the default branch, the files
and commits it brought in, and links from fix PRs to the PRs whose lines
they changed. ``repos`` records the last commit synced, so a sync only
reads what merged since.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("repos", sa.Column("history_synced_sha", sa.String(64), nullable=True))
    op.add_column(
        "repos", sa.Column("history_synced_at", sa.DateTime(timezone=True), nullable=True)
    )

    def repo_fk() -> sa.ForeignKey:
        return sa.ForeignKey("repos.id", ondelete="CASCADE")

    op.create_table(
        "history_prs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), repo_fk(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("branch", sa.Text(), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("merge_sha", sa.String(64), nullable=False),
        sa.Column("base_sha", sa.String(64), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("author", sa.Text(), nullable=False),
        sa.Column("files_changed", sa.Integer(), nullable=False),
        sa.Column("additions", sa.Integer(), nullable=False),
        sa.Column("deletions", sa.Integer(), nullable=False),
        sa.UniqueConstraint("repo_id", "number", name="uq_history_prs_repo_number"),
    )
    op.create_index("ix_history_prs_repo_merged_at", "history_prs", ["repo_id", "merged_at"])
    op.create_table(
        "history_pr_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), repo_fk(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("old_path", sa.Text(), nullable=True),
        sa.Column("additions", sa.Integer(), nullable=False),
        sa.Column("deletions", sa.Integer(), nullable=False),
    )
    op.create_index("ix_history_pr_files_repo_path", "history_pr_files", ["repo_id", "path"])
    op.create_index("ix_history_pr_files_repo_pr", "history_pr_files", ["repo_id", "pr_number"])
    op.create_table(
        "history_pr_commits",
        sa.Column(
            "repo_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("commit_sha", sa.String(64), primary_key=True),
        sa.Column("pr_number", sa.Integer(), nullable=False),
    )
    op.create_table(
        "defect_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "repo_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("repos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fix_pr", sa.Integer(), nullable=False),
        sa.Column("introducing_pr", sa.Integer(), nullable=True),
        sa.Column("share", sa.Float(), nullable=False),
        sa.Column("lines", postgresql.JSONB(), nullable=False),
    )
    op.create_index("ix_defect_links_repo_fix", "defect_links", ["repo_id", "fix_pr"])
    op.create_index(
        "ix_defect_links_repo_introducing", "defect_links", ["repo_id", "introducing_pr"]
    )


def downgrade() -> None:
    op.drop_table("defect_links")
    op.drop_table("history_pr_commits")
    op.drop_table("history_pr_files")
    op.drop_table("history_prs")
    op.drop_column("repos", "history_synced_at")
    op.drop_column("repos", "history_synced_sha")
