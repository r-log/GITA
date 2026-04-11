"""add outcomes table

Revision ID: d6e1f4a52b92
Revises: c5d9e2f34a81
Create Date: 2026-04-11 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd6e1f4a52b92'
down_revision: Union[str, None] = 'c5d9e2f34a81'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'outcomes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('repo_id', sa.Integer(), sa.ForeignKey('repositories.id'), nullable=False),
        sa.Column('agent_run_id', sa.Integer(), sa.ForeignKey('agent_runs.id'), nullable=False),
        sa.Column('outcome_type', sa.String(32), nullable=False),
        sa.Column('target_type', sa.String(16), nullable=False),
        sa.Column('target_number', sa.Integer(), nullable=True),
        sa.Column('predicted', postgresql.JSONB(), server_default='{}'),
        sa.Column('observed', postgresql.JSONB(), nullable=True),
        sa.Column('status', sa.String(16), nullable=False, server_default='pending'),
        sa.Column('scheduled_for', sa.DateTime(), nullable=False),
        sa.Column('checked_at', sa.DateTime(), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('agent_run_id', 'outcome_type', name='uq_outcomes_run_type'),
    )

    # Indexes
    op.create_index('ix_outcomes_repo_id', 'outcomes', ['repo_id'])
    op.create_index('ix_outcomes_agent_run_id', 'outcomes', ['agent_run_id'])
    op.create_index('ix_outcomes_pending_due', 'outcomes', ['status', 'scheduled_for'])
    op.create_index('ix_outcomes_target', 'outcomes', ['repo_id', 'target_type', 'target_number'])


def downgrade() -> None:
    op.drop_index('ix_outcomes_target', table_name='outcomes')
    op.drop_index('ix_outcomes_pending_due', table_name='outcomes')
    op.drop_index('ix_outcomes_agent_run_id', table_name='outcomes')
    op.drop_index('ix_outcomes_repo_id', table_name='outcomes')
    op.drop_table('outcomes')
