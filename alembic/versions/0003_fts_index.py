"""Full-text search GIN index on code_index.content

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-12 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Expression GIN index for full-text search over file content.
    # Uses 'simple' config (no stemming) which is better for code
    # identifiers than 'english'. The query planner picks this up
    # automatically when the WHERE clause matches the indexed expression.
    op.execute(
        "CREATE INDEX ix_code_index_content_fts "
        "ON code_index "
        "USING GIN(to_tsvector('simple', COALESCE(content, '')))"
    )


def downgrade() -> None:
    op.drop_index("ix_code_index_content_fts", table_name="code_index")
