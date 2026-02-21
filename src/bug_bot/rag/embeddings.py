from sentence_transformers import SentenceTransformer

from bug_bot.config import settings

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer(settings.rag_embedding_model)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed a list of document texts for storage."""
    model = _get_model()
    embeddings = model.encode(texts, normalize_embeddings=True)
    return embeddings.tolist()


def embed_query(query: str) -> list[float]:
    """Embed a single search query."""
    model = _get_model()
    embedding = model.encode([query], normalize_embeddings=True)
    return embedding[0].tolist()
