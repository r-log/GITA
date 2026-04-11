"""add content column to code_index

Revision ID: e7b2c8d94f13
Revises: d6e1f4a52b92
Create Date: 2026-04-11 12:00:00.000000

Stores raw source file content for meaningful files (filtered at ingest time)
so agents can pull granular code slices instead of re-fetching from GitHub.
Existing rows get content = NULL and will be backfilled on next re-index.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7b2c8d94f13'
down_revision: Union[str, None] = 'd6e1f4a52b92'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'code_index',
        sa.Column('content', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('code_index', 'content')
