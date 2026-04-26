"""Add auto_test_generation flag to repos table

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-26 00:00:00.000000

Per-repo opt-in for the Week 9 post-reindex auto-test-generation trigger.
Defaults to FALSE so existing repos get no behavior change until the user
explicitly opts in via ``gita index --auto-test-gen``.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "repos",
        sa.Column(
            "auto_test_generation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("repos", "auto_test_generation")
