import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.models.models import RagDocument


async def store_embeddings(
    session: AsyncSession,
    documents: list[dict],
) -> int:
    """Upsert document chunks with their embeddings.

    Each item in `documents` must have keys:
      source_type, source_id, chunk_text, chunk_metadata, embedding
    """
    now = datetime.now(timezone.utc)
    count = 0
    for doc in documents:
        row = RagDocument(
            id=uuid.uuid4(),
            source_type=doc["source_type"],
            source_id=doc["source_id"],
            chunk_text=doc["chunk_text"],
            chunk_metadata=doc.get("chunk_metadata"),
            embedding=doc["embedding"],
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        count += 1
    await session.commit()
    return count


async def similarity_search(
    session: AsyncSession,
    query_embedding: list[float],
    top_k: int = 5,
) -> list[dict]:
    """Return top-k most similar documents by cosine distance."""
    vec_literal = f"'[{','.join(str(v) for v in query_embedding)}]'"
    # Build the SQL with the vector literal inlined to avoid asyncpg's
    # conflict between :param bind syntax and PostgreSQL's :: cast operator.
    stmt = text(
        f"SELECT id, source_type, source_id, chunk_text, chunk_metadata,"
        f"       1 - (embedding <=> {vec_literal}::vector) AS similarity "
        f"FROM rag_documents "
        f"ORDER BY embedding <=> {vec_literal}::vector "
        f"LIMIT :top_k"
    )
    result = await session.execute(stmt, {"top_k": top_k})
    rows = result.fetchall()
    return [
        {
            "id": str(row[0]),
            "source_type": row[1],
            "source_id": row[2],
            "chunk_text": row[3],
            "chunk_metadata": row[4],
            "similarity": float(row[5]),
        }
        for row in rows
    ]


async def delete_by_source(
    session: AsyncSession,
    source_type: str,
    source_id: str,
) -> int:
    """Remove all chunks for a given source before re-indexing."""
    stmt = delete(RagDocument).where(
        RagDocument.source_type == source_type,
        RagDocument.source_id == source_id,
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.rowcount


async def get_stats(session: AsyncSession) -> dict:
    """Return indexing statistics."""
    total_q = await session.execute(
        select(func.count()).select_from(RagDocument)
    )
    total = int(total_q.scalar_one())

    by_type_q = await session.execute(
        select(RagDocument.source_type, func.count())
        .group_by(RagDocument.source_type)
    )
    by_type = {row[0]: row[1] for row in by_type_q.all()}

    last_q = await session.execute(
        select(func.max(RagDocument.updated_at))
    )
    last_indexed_at = last_q.scalar_one()

    return {
        "total_documents": total,
        "by_type": by_type,
        "last_indexed_at": last_indexed_at.isoformat() if last_indexed_at else None,
    }
