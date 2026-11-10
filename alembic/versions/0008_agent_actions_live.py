"""Partition agent_actions by liveness (shadow vs real side effects)

A shadow-mode row records that a decision was *observed*; nothing reached
GitHub. Sharing one unique key with live rows meant a shadow run
suppressed the same decision once writes were switched on. The unique
key now includes ``live``.

Backfill: ``shadow_logged`` rows are shadow. Shadow-mode *downgrades*
were also recorded with ``downgraded_*`` outcomes and can't be told
apart from live ones; they stay live (the conservative choice — at worst
a decision that only ran in shadow is not retried live).

Revision ID: 0008
Revises: 0007
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_actions",
        sa.Column("live", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("UPDATE agent_actions SET live = false WHERE outcome = 'shadow_logged'")
    op.drop_constraint("uq_agent_actions_signature", "agent_actions", type_="unique")
    op.create_unique_constraint(
        "uq_agent_actions_signature",
        "agent_actions",
        ["repo_name", "agent", "action", "signature", "live"],
    )


def downgrade() -> None:
    op.execute("DELETE FROM agent_actions WHERE live = false")
    op.drop_constraint("uq_agent_actions_signature", "agent_actions", type_="unique")
    op.create_unique_constraint(
        "uq_agent_actions_signature",
        "agent_actions",
        ["repo_name", "agent", "action", "signature"],
    )
    op.drop_column("agent_actions", "live")
