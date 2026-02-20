"""add_rag_documents_table

Revision ID: f8a2b3c4d5e6
Revises: e7b3d8f2a91
Create Date: 2026-02-21 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "f8a2b3c4d5e6"
down_revision: Union[str, None] = "60b46cd533cf"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
        CREATE TABLE rag_documents (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            source_type VARCHAR(30) NOT NULL,
            source_id VARCHAR(100) NOT NULL,
            chunk_text TEXT NOT NULL,
            chunk_metadata JSONB,
            embedding vector(384) NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    """)

    op.create_index("idx_rag_documents_source", "rag_documents", ["source_type", "source_id"])

    op.execute("""
        CREATE INDEX idx_rag_documents_embedding_hnsw
        ON rag_documents
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
    """)


def downgrade() -> None:
    op.drop_table("rag_documents")
    op.execute("DROP EXTENSION IF EXISTS vector")
