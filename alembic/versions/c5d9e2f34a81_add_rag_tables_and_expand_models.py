"""add RAG tables (events, commits, comments, reviews, diffs) and expand issues/pull_requests

Revision ID: c5d9e2f34a81
Revises: b4e8f1a23c57
Create Date: 2026-04-07 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c5d9e2f34a81'
down_revision: Union[str, None] = 'b4e8f1a23c57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── New tables ────────────────────────────────────────────────

    op.create_table(
        'events',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('repo_id', sa.Integer(), sa.ForeignKey('repositories.id'), nullable=False),
        sa.Column('delivery_id', sa.String(50), unique=True, nullable=False),
        sa.Column('event_type', sa.String(50), nullable=False),
        sa.Column('action', sa.String(50), nullable=True),
        sa.Column('sender_login', sa.String(100), nullable=True),
        sa.Column('target_type', sa.String(20), nullable=True),
        sa.Column('target_number', sa.Integer(), nullable=True),
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        sa.Column('received_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_event_repo_type', 'events', ['repo_id', 'event_type'])
    op.create_index('ix_event_repo_target', 'events', ['repo_id', 'target_type', 'target_number'])

    op.create_table(
        'commits',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('repo_id', sa.Integer(), sa.ForeignKey('repositories.id'), nullable=False),
        sa.Column('sha', sa.String(40), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('author_name', sa.String(200), nullable=True),
        sa.Column('author_email', sa.String(200), nullable=True),
        sa.Column('author_login', sa.String(100), nullable=True),
        sa.Column('committed_at', sa.DateTime(), nullable=True),
        sa.Column('files_added', postgresql.JSONB(), server_default='[]'),
        sa.Column('files_modified', postgresql.JSONB(), server_default='[]'),
        sa.Column('files_removed', postgresql.JSONB(), server_default='[]'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('repo_id', 'sha'),
    )
    op.create_index('ix_commit_repo_author', 'commits', ['repo_id', 'author_login'])
    op.create_index('ix_commit_committed_at', 'commits', ['repo_id', 'committed_at'])

    op.create_table(
        'comments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('repo_id', sa.Integer(), sa.ForeignKey('repositories.id'), nullable=False),
        sa.Column('github_id', sa.BigInteger(), unique=True, nullable=False),
        sa.Column('target_type', sa.String(10), nullable=False),
        sa.Column('target_number', sa.Integer(), nullable=False),
        sa.Column('author_login', sa.String(100), nullable=True),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('is_bot', sa.Boolean(), server_default='false'),
        sa.Column('github_created_at', sa.DateTime(), nullable=True),
        sa.Column('github_updated_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_comment_repo_target', 'comments', ['repo_id', 'target_type', 'target_number'])

    op.create_table(
        'reviews',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('repo_id', sa.Integer(), sa.ForeignKey('repositories.id'), nullable=False),
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('github_id', sa.BigInteger(), unique=True, nullable=False),
        sa.Column('author_login', sa.String(100), nullable=True),
        sa.Column('state', sa.String(30), nullable=False),
        sa.Column('body', sa.Text(), nullable=True),
        sa.Column('submitted_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_review_repo_pr', 'reviews', ['repo_id', 'pr_number'])

    op.create_table(
        'diffs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('repo_id', sa.Integer(), sa.ForeignKey('repositories.id'), nullable=False),
        sa.Column('pr_number', sa.Integer(), nullable=False),
        sa.Column('head_sha', sa.String(40), nullable=False),
        sa.Column('diff_text', sa.Text(), nullable=True),
        sa.Column('diff_size', sa.Integer(), server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint('repo_id', 'pr_number', 'head_sha', name='uq_diff_repo_pr_sha'),
    )
    op.create_index('ix_diff_repo_pr', 'diffs', ['repo_id', 'pr_number'])

    # ── Expand existing tables ────────────────────────────────────

    # issues: add body, author, github_created_at, closed_at
    op.add_column('issues', sa.Column('body', sa.Text(), nullable=True))
    op.add_column('issues', sa.Column('author', sa.String(100), nullable=True))
    op.add_column('issues', sa.Column('github_created_at', sa.DateTime(), nullable=True))
    op.add_column('issues', sa.Column('closed_at', sa.DateTime(), nullable=True))

    # pull_requests: add body, base_branch, head_branch, github_created_at, merged_at, merged_by, commit_count
    op.add_column('pull_requests', sa.Column('body', sa.Text(), nullable=True))
    op.add_column('pull_requests', sa.Column('base_branch', sa.String(200), nullable=True))
    op.add_column('pull_requests', sa.Column('head_branch', sa.String(200), nullable=True))
    op.add_column('pull_requests', sa.Column('github_created_at', sa.DateTime(), nullable=True))
    op.add_column('pull_requests', sa.Column('merged_at', sa.DateTime(), nullable=True))
    op.add_column('pull_requests', sa.Column('merged_by', sa.String(100), nullable=True))
    op.add_column('pull_requests', sa.Column('commit_count', sa.Integer(), nullable=True))


def downgrade() -> None:
    # Drop new columns from existing tables
    op.drop_column('pull_requests', 'commit_count')
    op.drop_column('pull_requests', 'merged_by')
    op.drop_column('pull_requests', 'merged_at')
    op.drop_column('pull_requests', 'github_created_at')
    op.drop_column('pull_requests', 'head_branch')
    op.drop_column('pull_requests', 'base_branch')
    op.drop_column('pull_requests', 'body')

    op.drop_column('issues', 'closed_at')
    op.drop_column('issues', 'github_created_at')
    op.drop_column('issues', 'author')
    op.drop_column('issues', 'body')

    # Drop new tables
    op.drop_table('diffs')
    op.drop_table('reviews')
    op.drop_table('comments')
    op.drop_table('commits')
    op.drop_table('events')
