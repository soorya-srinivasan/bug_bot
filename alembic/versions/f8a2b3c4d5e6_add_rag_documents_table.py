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
    from sqlalchemy import text

    conn = op.get_bind()

    # Try to install pgvector. Use a savepoint so that if the extension is not
    # available on this system the transaction is not aborted — we roll back to
    # the savepoint and create the table with a TEXT placeholder instead, allowing
    # the rest of the migration chain to proceed normally.
    vector_available = False
    conn.execute(text("SAVEPOINT before_vector"))
    try:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("RELEASE SAVEPOINT before_vector"))
        vector_available = True
    except Exception:
        conn.execute(text("ROLLBACK TO SAVEPOINT before_vector"))

    if vector_available:
        op.execute("""
            CREATE TABLE IF NOT EXISTS rag_documents (
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
        op.execute("""
            CREATE INDEX IF NOT EXISTS idx_rag_documents_embedding_hnsw
            ON rag_documents
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """)
    else:
        # pgvector unavailable: create table with TEXT placeholder for embedding
        op.execute("""
            CREATE TABLE IF NOT EXISTS rag_documents (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                source_type VARCHAR(30) NOT NULL,
                source_id VARCHAR(100) NOT NULL,
                chunk_text TEXT NOT NULL,
                chunk_metadata JSONB,
                embedding TEXT,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            )
        """)

    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_documents_source ON rag_documents (source_type, source_id)"
    )


def downgrade() -> None:
    op.drop_table("rag_documents")
    op.execute("DROP EXTENSION IF EXISTS vector")
