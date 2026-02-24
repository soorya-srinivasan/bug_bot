import logging

from sentence_transformers import CrossEncoder

from bug_bot.config import settings

logger = logging.getLogger(__name__)

_reranker: CrossEncoder | None = None


def _get_reranker() -> CrossEncoder:
    global _reranker
    if _reranker is None:
        _reranker = CrossEncoder(settings.rag_rerank_model, max_length=512)
    return _reranker


def rerank(query: str, documents: list[dict], top_k: int | None = None) -> list[dict]:
    """Rerank documents using a cross-encoder model.

    Scores each (query, chunk_text) pair and returns the top_k by score.
    """
    if not documents:
        return []

    top_k = top_k or settings.rag_rerank_top_k
    reranker = _get_reranker()

    pairs = [(query, doc["chunk_text"]) for doc in documents]
    scores = reranker.predict(pairs)

    for doc, score in zip(documents, scores):
        doc["rerank_score"] = float(score)

    ranked = sorted(documents, key=lambda d: d["rerank_score"], reverse=True)
    return ranked[:top_k]
