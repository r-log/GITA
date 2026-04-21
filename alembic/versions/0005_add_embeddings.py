"""Add vector embedding column to code_index

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enable pgvector extension (requires pgvector/pgvector Docker image).
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # Add embedding column — 1536 dimensions (OpenAI text-embedding-3-small).
    # Nullable: files indexed before embeddings are enabled will have NULL.
    op.execute(
        "ALTER TABLE code_index ADD COLUMN embedding vector(1536)"
    )

    # HNSW index for cosine similarity search.
    # HNSW is preferred over IVFFlat for small-to-medium repos (<10K files)
    # because it doesn't require a training step.
    op.execute(
        "CREATE INDEX ix_code_index_embedding "
        "ON code_index "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.drop_index("ix_code_index_embedding", table_name="code_index")
    op.execute("ALTER TABLE code_index DROP COLUMN embedding")
    op.execute("DROP EXTENSION IF EXISTS vector")
