"""initial schema: repos, code_index, import_edges

Revision ID: 0001
Revises:
Create Date: 2026-04-11 22:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "repos",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("root_path", sa.Text(), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("name", name="uq_repos_name"),
    )

    op.create_table(
        "code_index",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("line_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("indexed_at_sha", sa.String(length=64), nullable=True),
        sa.Column(
            "structure",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("repo_id", "file_path", name="uq_code_index_repo_file"),
    )
    op.create_index("ix_code_index_repo_id", "code_index", ["repo_id"])
    op.create_index("ix_code_index_language", "code_index", ["language"])

    op.create_table(
        "import_edges",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("repo_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("src_file", sa.Text(), nullable=False),
        sa.Column("dst_file", sa.Text(), nullable=True),
        sa.Column("raw_import", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["repo_id"], ["repos.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_import_edges_repo_src", "import_edges", ["repo_id", "src_file"]
    )
    op.create_index(
        "ix_import_edges_repo_dst", "import_edges", ["repo_id", "dst_file"]
    )


def downgrade() -> None:
    op.drop_index("ix_import_edges_repo_dst", table_name="import_edges")
    op.drop_index("ix_import_edges_repo_src", table_name="import_edges")
    op.drop_table("import_edges")
    op.drop_index("ix_code_index_language", table_name="code_index")
    op.drop_index("ix_code_index_repo_id", table_name="code_index")
    op.drop_table("code_index")
    op.drop_table("repos")
