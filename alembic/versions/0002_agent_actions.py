"""agent_actions table for decision dedupe

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-12 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("repo_name", sa.String(length=255), nullable=False),
        sa.Column("agent", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("signature", sa.String(length=64), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "repo_name",
            "agent",
            "action",
            "signature",
            name="uq_agent_actions_signature",
        ),
    )
    op.create_index(
        "ix_agent_actions_repo_name", "agent_actions", ["repo_name"]
    )


def downgrade() -> None:
    op.drop_index("ix_agent_actions_repo_name", table_name="agent_actions")
    op.drop_table("agent_actions")
