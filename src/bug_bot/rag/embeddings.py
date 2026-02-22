import hashlib

from sentence_transformers import SentenceTransformer

from bug_bot.config import settings

_model: SentenceTransformer | None = None
_query_cache: dict[str, list[float]] = {}
_QUERY_CACHE_MAX = 1000


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(settings.rag_embedding_model)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed document texts for storage."""
    model = _get_model()
    embeddings = model.encode(texts, normalize_embeddings=True, batch_size=32)
    return embeddings.tolist()


def embed_query(query: str) -> list[float]:
    """Embed a single search query with instruction prefix and caching."""
    cache_key = hashlib.md5(query.encode()).hexdigest()
    if cache_key in _query_cache:
        return _query_cache[cache_key]

    model = _get_model()
    # BGE models use a query instruction prefix for better retrieval
    prefixed = f"Represent this sentence for searching relevant passages: {query}"
    embedding = model.encode([prefixed], normalize_embeddings=True)
    result = embedding[0].tolist()

    if len(_query_cache) >= _QUERY_CACHE_MAX:
        _query_cache.clear()
    _query_cache[cache_key] = result
    return result
