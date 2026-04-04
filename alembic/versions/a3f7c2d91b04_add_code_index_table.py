"""add code_index table

Revision ID: a3f7c2d91b04
Revises: 1ec4ee5b6a75
Create Date: 2026-04-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3f7c2d91b04'
down_revision: Union[str, None] = '1ec4ee5b6a75'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'code_index',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('repo_id', sa.Integer(), sa.ForeignKey('repositories.id'), nullable=False),
        sa.Column('file_path', sa.String(500), nullable=False),
        sa.Column('language', sa.String(30), nullable=False),
        sa.Column('size_bytes', sa.Integer(), server_default='0'),
        sa.Column('line_count', sa.Integer(), server_default='0'),
        sa.Column('structure', postgresql.JSONB(), server_default='{}'),
        sa.Column('content_hash', sa.String(64), nullable=False),
        sa.Column('indexed_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('repo_id', 'file_path', name='uq_code_index_repo_file'),
    )
    op.create_index('ix_code_index_repo_id', 'code_index', ['repo_id'])
    op.create_index('ix_code_index_language', 'code_index', ['language'])


def downgrade() -> None:
    op.drop_index('ix_code_index_language')
    op.drop_index('ix_code_index_repo_id')
    op.drop_table('code_index')
