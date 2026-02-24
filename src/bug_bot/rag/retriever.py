"""Hybrid retriever: BM25 + semantic search fused with Reciprocal Rank Fusion."""

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.config import settings
from bug_bot.db.session import async_session as session_factory
from bug_bot.rag.embeddings import embed_query
from bug_bot.rag.vectorstore import bm25_search, similarity_search

logger = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    result_lists: list[list[dict]],
    k: int = 60,
    weights: list[float] | None = None,
) -> list[dict]:
    """Merge multiple ranked result lists using Reciprocal Rank Fusion.

    Each result is scored: ``sum_over_lists(weight / (k + rank + 1))``.
    Results are deduplicated by ``id`` and sorted by fused score descending.
    """
    if weights is None:
        weights = [1.0] * len(result_lists)

    fused_scores: dict[str, float] = {}
    doc_map: dict[str, dict] = {}

    for result_list, weight in zip(result_lists, weights):
        for rank, doc in enumerate(result_list):
            doc_id = doc["id"]
            fused_scores[doc_id] = fused_scores.get(doc_id, 0.0) + weight / (k + rank + 1)
            if doc_id not in doc_map:
                doc_map[doc_id] = doc

    sorted_ids = sorted(fused_scores, key=lambda x: fused_scores[x], reverse=True)
    results = []
    for doc_id in sorted_ids:
        doc = doc_map[doc_id].copy()
        doc["rrf_score"] = fused_scores[doc_id]
        results.append(doc)
    return results


async def _run_semantic(
    query_embedding: list[float], retrieval_k: int, filters: dict | None,
) -> list[dict]:
    """Run semantic search in its own session (safe for asyncio.gather)."""
    async with session_factory() as session:
        return await similarity_search(session, query_embedding, top_k=retrieval_k, filters=filters)


async def _run_bm25(
    query: str, retrieval_k: int, filters: dict | None,
) -> list[dict]:
    """Run BM25 search in its own session (safe for asyncio.gather)."""
    async with session_factory() as session:
        return await bm25_search(session, query, top_k=retrieval_k, filters=filters)


async def hybrid_retrieve(
    session: AsyncSession,
    query: str,
    top_k: int | None = None,
    filters: dict | None = None,
) -> list[dict]:
    """Run BM25 and semantic search in parallel, fuse results with RRF.

    Each search uses its own database session so they can execute concurrently
    without violating SQLAlchemy's single-session concurrency constraint.

    Returns up to ``3 * rerank_top_k`` candidates for downstream reranking.
    """
    # When top_k is overridden (e.g. list queries), fetch at least that many from DB
    retrieval_k = max(settings.rag_retrieval_k, (top_k or 0))

    query_embedding = embed_query(query)

    # Run both searches concurrently — each gets its own session from the pool
    semantic_results, bm25_results = await asyncio.gather(
        _run_semantic(query_embedding, retrieval_k, filters),
        _run_bm25(query, retrieval_k, filters),
    )

    logger.debug(
        "Hybrid search: %d semantic, %d BM25 results",
        len(semantic_results),
        len(bm25_results),
    )

    fused = reciprocal_rank_fusion(
        [semantic_results, bm25_results],
        weights=[settings.rag_semantic_weight, settings.rag_bm25_weight],
    )

    final_k = top_k or settings.rag_rerank_top_k
    # When top_k is already large (list query), don't multiply further
    headroom = final_k * 3 if final_k <= settings.rag_rerank_top_k else final_k
    return fused[:headroom]
