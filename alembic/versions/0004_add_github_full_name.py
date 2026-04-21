"""Add github_full_name column to repos table

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-21 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repos",
        sa.Column("github_full_name", sa.String(255), nullable=True),
    )
    # Partial unique index — only enforced when the column is populated.
    # CLI-only repos (no GitHub origin) leave it NULL and skip the constraint.
    op.execute(
        "CREATE UNIQUE INDEX ix_repos_github_full_name "
        "ON repos (github_full_name) "
        "WHERE github_full_name IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_repos_github_full_name", table_name="repos")
    op.drop_column("repos", "github_full_name")
