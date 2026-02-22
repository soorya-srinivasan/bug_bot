"""upgrade_rag_hybrid_search

Revision ID: a1b2c3d4e5f6
Revises: d5e6f7a8b9c0
Create Date: 2026-02-22 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Drop old HNSW index on 384-dim embedding
    op.execute("DROP INDEX IF EXISTS idx_rag_documents_embedding_hnsw")

    # 2. Widen embedding column from vector(384) to vector(768)
    #    Existing rows get NULLed — a full reindex is required after migration.
    op.execute(
        "ALTER TABLE rag_documents "
        "ALTER COLUMN embedding DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE rag_documents "
        "ALTER COLUMN embedding TYPE vector(768) USING NULL"
    )

    # 3. Recreate HNSW index for 768-dim vectors with better ef_construction
    op.execute(
        "CREATE INDEX idx_rag_documents_embedding_hnsw "
        "ON rag_documents "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 200)"
    )

    # 4. Add tsvector column for BM25 full-text search
    op.execute(
        "ALTER TABLE rag_documents "
        "ADD COLUMN IF NOT EXISTS search_vector tsvector"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_documents_fts "
        "ON rag_documents USING gin(search_vector)"
    )

    # 5. Auto-populate tsvector via trigger
    op.execute("""
        CREATE OR REPLACE FUNCTION update_rag_search_vector()
        RETURNS trigger AS $$
        BEGIN
            NEW.search_vector := to_tsvector('english', NEW.chunk_text);
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_rag_search_vector
        BEFORE INSERT OR UPDATE OF chunk_text ON rag_documents
        FOR EACH ROW EXECUTE FUNCTION update_rag_search_vector()
    """)

    # 6. Add context_prefix column for contextual retrieval
    op.execute(
        "ALTER TABLE rag_documents "
        "ADD COLUMN IF NOT EXISTS context_prefix TEXT"
    )

    # 7. Add denormalized metadata columns for fast filtering
    op.execute(
        "ALTER TABLE rag_documents "
        "ADD COLUMN IF NOT EXISTS severity VARCHAR(10)"
    )
    op.execute(
        "ALTER TABLE rag_documents "
        "ADD COLUMN IF NOT EXISTS status VARCHAR(20)"
    )
    op.execute(
        "ALTER TABLE rag_documents "
        "ADD COLUMN IF NOT EXISTS service_name VARCHAR(100)"
    )
    op.execute(
        "ALTER TABLE rag_documents "
        "ADD COLUMN IF NOT EXISTS created_date DATE"
    )

    # 8. Indexes on filter columns
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_documents_severity "
        "ON rag_documents(severity)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_documents_status "
        "ON rag_documents(status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_documents_service "
        "ON rag_documents(service_name)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_rag_documents_created_date "
        "ON rag_documents(created_date)"
    )


def downgrade() -> None:
    # Drop filter indexes
    op.execute("DROP INDEX IF EXISTS idx_rag_documents_created_date")
    op.execute("DROP INDEX IF EXISTS idx_rag_documents_service")
    op.execute("DROP INDEX IF EXISTS idx_rag_documents_status")
    op.execute("DROP INDEX IF EXISTS idx_rag_documents_severity")

    # Drop filter columns
    op.execute("ALTER TABLE rag_documents DROP COLUMN IF EXISTS created_date")
    op.execute("ALTER TABLE rag_documents DROP COLUMN IF EXISTS service_name")
    op.execute("ALTER TABLE rag_documents DROP COLUMN IF EXISTS status")
    op.execute("ALTER TABLE rag_documents DROP COLUMN IF EXISTS severity")

    # Drop context_prefix
    op.execute("ALTER TABLE rag_documents DROP COLUMN IF EXISTS context_prefix")

    # Drop tsvector trigger and column
    op.execute("DROP TRIGGER IF EXISTS trg_rag_search_vector ON rag_documents")
    op.execute("DROP FUNCTION IF EXISTS update_rag_search_vector()")
    op.execute("DROP INDEX IF EXISTS idx_rag_documents_fts")
    op.execute("ALTER TABLE rag_documents DROP COLUMN IF EXISTS search_vector")

    # Revert embedding back to vector(384)
    op.execute("DROP INDEX IF EXISTS idx_rag_documents_embedding_hnsw")
    op.execute(
        "ALTER TABLE rag_documents "
        "ALTER COLUMN embedding DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE rag_documents "
        "ALTER COLUMN embedding TYPE vector(384) USING NULL"
    )
    op.execute(
        "ALTER TABLE rag_documents "
        "ALTER COLUMN embedding SET NOT NULL"
    )
    op.execute(
        "CREATE INDEX idx_rag_documents_embedding_hnsw "
        "ON rag_documents "
        "USING hnsw (embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64)"
    )
