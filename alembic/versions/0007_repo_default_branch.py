"""Add default_branch column to repos table

Revision ID: 0007
Revises: 0006
Create Date: 2026-04-26 00:00:00.000000

Records each indexed repo's default branch so the auto-test-generation
trigger (Week 9) can target the right base branch when opening PRs,
instead of hardcoding ``"main"``. Server-default of ``"main"`` covers
the common case for existing rows; ``index_repository`` auto-corrects
for non-main repos on the next index run via
``discover_default_branch``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repos",
        sa.Column(
            "default_branch",
            sa.String(255),
            nullable=False,
            server_default="main",
        ),
    )


def downgrade() -> None:
    op.drop_column("repos", "default_branch")
