import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, select, text, func
from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.models.models import RagDocument


def _build_filter_clauses(
    filters: dict | None,
    params: dict,
) -> list[str]:
    """Build SQL WHERE clauses from a filter dict."""
    clauses: list[str] = []
    if not filters:
        return clauses
    if filters.get("severity"):
        clauses.append("severity = :f_severity")
        params["f_severity"] = filters["severity"]
    if filters.get("status"):
        clauses.append("status = :f_status")
        params["f_status"] = filters["status"]
    if filters.get("service_name"):
        clauses.append("service_name = :f_service_name")
        params["f_service_name"] = filters["service_name"]
    if filters.get("source_type"):
        clauses.append("source_type = :f_source_type")
        params["f_source_type"] = filters["source_type"]
    return clauses


async def store_embeddings(
    session: AsyncSession,
    documents: list[dict],
) -> int:
    """Upsert document chunks with their embeddings.

    Each item in ``documents`` must have keys:
      source_type, source_id, chunk_text, chunk_metadata, embedding
    Optional keys: context_prefix, severity, status, service_name, created_date
    """
    now = datetime.now(timezone.utc)
    count = 0
    for doc in documents:
        row = RagDocument(
            id=uuid.uuid4(),
            source_type=doc["source_type"],
            source_id=doc["source_id"],
            chunk_text=doc["chunk_text"],
            context_prefix=doc.get("context_prefix"),
            chunk_metadata=doc.get("chunk_metadata"),
            embedding=doc["embedding"],
            severity=doc.get("severity"),
            status=doc.get("status"),
            service_name=doc.get("service_name"),
            created_date=doc.get("created_date"),
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        count += 1
    await session.commit()
    return count


async def lookup_by_bug_id(
    session: AsyncSession,
    bug_id: str,
) -> list[dict]:
    """Return all indexed documents for a specific bug ID (exact match)."""
    stmt = text(
        "SELECT id, source_type, source_id, chunk_text, chunk_metadata "
        "FROM rag_documents "
        "WHERE source_id = :bug_id OR source_id LIKE :bug_id_prefix "
        "ORDER BY source_type"
    )
    result = await session.execute(
        stmt, {"bug_id": bug_id, "bug_id_prefix": f"{bug_id}:%"}
    )
    rows = result.fetchall()
    return [
        {
            "id": str(row[0]),
            "source_type": row[1],
            "source_id": row[2],
            "chunk_text": row[3],
            "chunk_metadata": row[4],
            "similarity": 1.0,
        }
        for row in rows
    ]


async def similarity_search(
    session: AsyncSession,
    query_embedding: list[float],
    top_k: int = 5,
    filters: dict | None = None,
) -> list[dict]:
    """Return top-k most similar documents by cosine distance with optional filtering."""
    vec_literal = f"'[{','.join(str(v) for v in query_embedding)}]'"
    params: dict = {"top_k": top_k}
    filter_clauses = _build_filter_clauses(filters, params)
    # Always exclude rows with NULL embeddings (e.g. after a dimension migration)
    filter_clauses.append("embedding IS NOT NULL")
    where_sql = "WHERE " + " AND ".join(filter_clauses)

    stmt = text(
        f"SELECT id, source_type, source_id, chunk_text, chunk_metadata,"
        f"       1 - (embedding <=> {vec_literal}::vector) AS similarity "
        f"FROM rag_documents "
        f"{where_sql} "
        f"ORDER BY embedding <=> {vec_literal}::vector "
        f"LIMIT :top_k"
    )
    result = await session.execute(stmt, params)
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


async def bm25_search(
    session: AsyncSession,
    query: str,
    top_k: int = 20,
    filters: dict | None = None,
) -> list[dict]:
    """Full-text search using PostgreSQL tsvector/tsquery."""
    params: dict = {"query": query, "top_k": top_k}
    filter_clauses = _build_filter_clauses(filters, params)
    # The tsvector match is always required; also skip rows with NULL search_vector
    all_clauses = [
        "search_vector IS NOT NULL",
        "search_vector @@ plainto_tsquery('english', :query)",
    ] + filter_clauses
    where_sql = " AND ".join(all_clauses)

    stmt = text(
        f"SELECT id, source_type, source_id, chunk_text, chunk_metadata, "
        f"       ts_rank(search_vector, plainto_tsquery('english', :query)) AS rank "
        f"FROM rag_documents "
        f"WHERE {where_sql} "
        f"ORDER BY rank DESC "
        f"LIMIT :top_k"
    )
    result = await session.execute(stmt, params)
    rows = result.fetchall()
    return [
        {
            "id": str(row[0]),
            "source_type": row[1],
            "source_id": row[2],
            "chunk_text": row[3],
            "chunk_metadata": row[4],
            "bm25_rank": float(row[5]),
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
