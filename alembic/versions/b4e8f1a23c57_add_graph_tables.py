"""add graph_nodes, graph_edges, pr_file_changes tables

Revision ID: b4e8f1a23c57
Revises: a3f7c2d91b04
Create Date: 2026-04-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b4e8f1a23c57'
down_revision: Union[str, None] = 'a3f7c2d91b04'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── graph_nodes ──────────────────────────────────────────────
    op.create_table(
        'graph_nodes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('repo_id', sa.Integer(), sa.ForeignKey('repositories.id'), nullable=False),
        sa.Column('node_type', sa.String(20), nullable=False),
        sa.Column('qualified_name', sa.String(500), nullable=False),
        sa.Column('file_path', sa.String(500), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('language', sa.String(30), nullable=False),
        sa.Column('line_number', sa.Integer(), nullable=True),
        sa.Column('extra', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('repo_id', 'qualified_name', name='uq_graph_node_repo_qname'),
    )
    op.create_index('ix_graph_node_repo_type', 'graph_nodes', ['repo_id', 'node_type'])
    op.create_index('ix_graph_node_repo_file', 'graph_nodes', ['repo_id', 'file_path'])

    # ── graph_edges ──────────────────────────────────────────────
    op.create_table(
        'graph_edges',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('repo_id', sa.Integer(), sa.ForeignKey('repositories.id'), nullable=False),
        sa.Column('source_node_id', sa.Integer(),
                  sa.ForeignKey('graph_nodes.id', ondelete='CASCADE'), nullable=False),
        sa.Column('target_node_id', sa.Integer(),
                  sa.ForeignKey('graph_nodes.id', ondelete='CASCADE'), nullable=True),
        sa.Column('edge_type', sa.String(30), nullable=False),
        sa.Column('target_entity_type', sa.String(20), nullable=True),
        sa.Column('target_entity_id', sa.Integer(), nullable=True),
        sa.Column('confidence', sa.Float(), server_default='1.0'),
        sa.Column('extra', postgresql.JSONB(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_graph_edge_repo_type', 'graph_edges', ['repo_id', 'edge_type'])
    op.create_index('ix_graph_edge_source', 'graph_edges', ['source_node_id'])
    op.create_index('ix_graph_edge_target', 'graph_edges', ['target_node_id'])
    op.create_index('ix_graph_edge_entity', 'graph_edges',
                    ['repo_id', 'target_entity_type', 'target_entity_id'])

    # ── pr_file_changes ──────────────────────────────────────────
    op.create_table(
        'pr_file_changes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('repo_id', sa.Integer(), sa.ForeignKey('repositories.id'), nullable=False),
        sa.Column('pr_id', sa.Integer(), sa.ForeignKey('pull_requests.id'), nullable=False),
        sa.Column('file_path', sa.String(500), nullable=False),
        sa.Column('change_type', sa.String(20), nullable=False),
        sa.Column('additions', sa.Integer(), server_default='0'),
        sa.Column('deletions', sa.Integer(), server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('pr_id', 'file_path', name='uq_pr_file_change_pr_file'),
    )
    op.create_index('ix_pr_file_change_repo_file', 'pr_file_changes', ['repo_id', 'file_path'])


def downgrade() -> None:
    op.drop_index('ix_pr_file_change_repo_file')
    op.drop_table('pr_file_changes')

    op.drop_index('ix_graph_edge_entity')
    op.drop_index('ix_graph_edge_target')
    op.drop_index('ix_graph_edge_source')
    op.drop_index('ix_graph_edge_repo_type')
    op.drop_table('graph_edges')

    op.drop_index('ix_graph_node_repo_file')
    op.drop_index('ix_graph_node_repo_type')
    op.drop_table('graph_nodes')
