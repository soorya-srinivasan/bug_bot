import hashlib
import time

from bug_bot.config import settings

_response_cache: dict[str, tuple[float, dict]] = {}
_MAX_CACHE_SIZE = 500
_EVICT_COUNT = 100


def _cache_key(message: str, history: list[dict] | None) -> str:
    """Generate cache key from message + recent history."""
    parts = [message]
    if history:
        for msg in history[-4:]:
            parts.append(f"{msg['role']}:{msg['content'][:100]}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def get_cached_response(message: str, history: list[dict] | None = None) -> dict | None:
    """Return cached response if it exists and hasn't expired."""
    key = _cache_key(message, history)
    if key in _response_cache:
        ts, result = _response_cache[key]
        if time.time() - ts < settings.rag_cache_ttl_seconds:
            return result
        del _response_cache[key]
    return None


def set_cached_response(
    message: str,
    result: dict,
    history: list[dict] | None = None,
) -> None:
    """Cache a response with TTL."""
    key = _cache_key(message, history)
    if len(_response_cache) >= _MAX_CACHE_SIZE:
        oldest = sorted(_response_cache, key=lambda k: _response_cache[k][0])[:_EVICT_COUNT]
        for k in oldest:
            del _response_cache[k]
    _response_cache[key] = (time.time(), result)
