"""Make repos.github_full_name unique case-insensitively

Lookups compare ``lower(github_full_name)`` (GitHub names are
case-insensitive), but uniqueness was case-sensitive, so "Owner/Repo" and
"owner/repo" could both exist and make the lookup ambiguous. Fails if the
table already holds such a pair; resolve those rows first.

Revision ID: 0009
Revises: 0008
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_repos_github_full_name", table_name="repos")
    op.execute(
        "CREATE UNIQUE INDEX ix_repos_github_full_name_lower "
        "ON repos (lower(github_full_name)) "
        "WHERE github_full_name IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_repos_github_full_name_lower", table_name="repos")
    op.execute(
        "CREATE UNIQUE INDEX ix_repos_github_full_name "
        "ON repos (github_full_name) "
        "WHERE github_full_name IS NOT NULL"
    )
