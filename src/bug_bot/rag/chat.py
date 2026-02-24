import json
import logging
import re
from typing import AsyncGenerator

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.config import settings
from bug_bot.rag.cache import get_cached_response, set_cached_response
from bug_bot.rag.live_context import fetch_oncall_context, fetch_service_mappings_context
from bug_bot.rag.query_rewriter import rewrite_query
from bug_bot.rag.reranker import rerank
from bug_bot.rag.retriever import hybrid_retrieve
from bug_bot.rag.vectorstore import lookup_by_bug_id

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are Bug Bot Assistant, an AI helper for the ShopTech engineering team.
You answer questions about bugs, investigations, incidents, on-call schedules,
and service ownership using the context provided below.

Rules:
- Base your answers ONLY on the retrieved context. Do not make up information.
- Always cite the relevant Bug ID (e.g., BUG-001) when referencing specific bugs.
- If the context does not contain enough information to answer, say so clearly.
- Be concise and technical. Engineers are your audience.
- When describing root causes or fixes, include relevant technical details from the context.
- When the user asks about a SPECIFIC bug ID, focus your answer on that bug only.
  Any context after "=== RELATED BUGS ===" is supplementary — mention related bugs
  in a separate section only if they add value, and never merge their details with
  the requested bug.
- If multiple bugs are relevant and no specific bug was requested, summarize each briefly.
- On-call and service mapping data under "=== CURRENT ON-CALL INFORMATION ===" and
  "=== SERVICE MAPPINGS ===" is live data. Use it to answer questions about who is
  on-call, service ownership, team assignments, and related topics.
"""

_CHAT_MODEL = "claude-haiku-4-5-20251001"

_ONCALL_KEYWORDS = re.compile(
    r"on[-\s]?call|oncall|who(?:'s| is) (?:on call|covering|paged)"
    r"|roster|rotation|schedule|override|pager|duty",
    re.IGNORECASE,
)

_SERVICE_KEYWORDS = re.compile(
    r"service[\s-]?(?:owner|mapping|team)|who owns|which team"
    r"|tech[\s-]?stack|repo(?:sitory)?[\s-]?for",
    re.IGNORECASE,
)

_BUG_ID_PATTERN = re.compile(r"BUG-\d+", re.IGNORECASE)

_SEVERITY_PATTERN = re.compile(r"\b(P[1-4]|critical|high|medium|low)\b", re.IGNORECASE)
_SEVERITY_MAP = {
    "critical": "P1", "high": "P2", "medium": "P3", "low": "P4",
    "p1": "P1", "p2": "P2", "p3": "P3", "p4": "P4",
}
_STATUS_PATTERN = re.compile(
    r"\b(open|investigating|escalated|resolved|closed|stale)\b", re.IGNORECASE,
)
_LIST_PATTERN = re.compile(
    r"\b(list|show|all|every|give me|how many|count)\b", re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _extract_bug_ids(message: str) -> list[str]:
    """Extract unique bug IDs from a message."""
    return list(dict.fromkeys(m.upper() for m in _BUG_ID_PATTERN.findall(message)))


def _extract_auto_filters(message: str) -> tuple[dict | None, int | None]:
    """Extract metadata filters and adaptive top_k from the user's query.

    Returns (filters_dict_or_None, top_k_override_or_None).
    """
    filters: dict = {}

    sev_match = _SEVERITY_PATTERN.search(message)
    if sev_match:
        raw = sev_match.group(1).lower()
        filters["severity"] = _SEVERITY_MAP.get(raw, raw.upper())

    status_match = _STATUS_PATTERN.search(message)
    if status_match:
        filters["status"] = status_match.group(1).lower()

    # When user wants a list/all and has a filter, return more results
    top_k_override = None
    if filters and _LIST_PATTERN.search(message):
        top_k_override = 50  # enough for a comprehensive list

    return (filters if filters else None), top_k_override


async def _fetch_exact_results(
    session: AsyncSession, mentioned_ids: list[str],
) -> tuple[list[dict], set[str]]:
    """Look up exact documents for mentioned bug IDs."""
    exact_results: list[dict] = []
    exact_bug_ids: set[str] = set()
    for bug_id in mentioned_ids:
        docs = await lookup_by_bug_id(session, bug_id)
        if docs:
            exact_results.extend(docs)
            exact_bug_ids.add(bug_id)
    return exact_results, exact_bug_ids


async def _build_context(
    session: AsyncSession,
    exact_results: list[dict],
    semantic_results: list[dict],
    wants_oncall: bool,
    wants_service: bool,
) -> tuple[list[str], list[dict]]:
    """Build context blocks and sources list from retrieval results."""
    context_blocks: list[str] = []
    sources: list[dict] = []

    if exact_results:
        for doc in exact_results:
            context_blocks.append(
                f"[{doc['source_type'].upper()} — {doc['source_id']}]\n{doc['chunk_text']}"
            )
            bug_id = doc["source_id"].split(":")[0] if ":" in doc["source_id"] else doc["source_id"]
            sources.append({
                "bug_id": bug_id,
                "source_type": doc["source_type"],
                "chunk_text": doc["chunk_text"],
                "similarity": doc.get("similarity", doc.get("rerank_score", 0.0)),
                "link": f"/bugs/{bug_id}",
            })

    if semantic_results:
        if exact_results:
            context_blocks.append("=== RELATED BUGS (from semantic search) ===")
        for doc in semantic_results:
            context_blocks.append(
                f"[{doc['source_type'].upper()} — {doc['source_id']}]\n{doc['chunk_text']}"
            )
            bug_id = doc["source_id"].split(":")[0] if ":" in doc["source_id"] else doc["source_id"]
            sources.append({
                "bug_id": bug_id,
                "source_type": doc["source_type"],
                "chunk_text": doc["chunk_text"],
                "similarity": doc.get("similarity", doc.get("rerank_score", doc.get("rrf_score", 0.0))),
                "link": f"/bugs/{bug_id}",
            })

    if wants_oncall:
        oncall_ctx = await fetch_oncall_context(session)
        if oncall_ctx:
            context_blocks.append(oncall_ctx)
            sources.append({
                "bug_id": "On-Call",
                "source_type": "oncall",
                "chunk_text": "Live on-call schedule data",
                "similarity": 1.0,
                "link": "/on-call",
            })

    if wants_service or wants_oncall:
        svc_ctx = await fetch_service_mappings_context(session)
        if svc_ctx:
            context_blocks.append(svc_ctx)
            if not wants_oncall:
                sources.append({
                    "bug_id": "Services",
                    "source_type": "service_mapping",
                    "chunk_text": "Live service mapping data",
                    "similarity": 1.0,
                    "link": "/service-team-mappings",
                })

    return context_blocks, sources


def _build_messages(
    context_blocks: list[str],
    message: str,
    conversation_history: list[dict] | None = None,
) -> list[dict]:
    """Build the message list for the Claude API call."""
    context_text = "\n\n---\n\n".join(context_blocks) if context_blocks else "(No relevant documents found)"
    messages = []
    if conversation_history:
        for msg in conversation_history:
            messages.append({"role": msg["role"], "content": msg["content"]})
    messages.append({
        "role": "user",
        "content": f"Context from bug database:\n\n{context_text}\n\n---\n\nQuestion: {message}",
    })
    return messages


# ---------------------------------------------------------------------------
# Core retrieval pipeline (shared by streaming and non-streaming)
# ---------------------------------------------------------------------------


async def _retrieve(
    session: AsyncSession,
    message: str,
    conversation_history: list[dict] | None = None,
    filters: dict | None = None,
) -> tuple[list[str], list[dict]]:
    """Run the full retrieval pipeline and return (context_blocks, sources)."""
    # 1. Query rewriting for multi-turn conversations
    search_query = await rewrite_query(message, conversation_history)

    # 2. Exact bug ID lookup
    mentioned_ids = _extract_bug_ids(message)
    exact_results, exact_bug_ids = await _fetch_exact_results(session, mentioned_ids)

    # 3. Detect intent
    wants_oncall = bool(_ONCALL_KEYWORDS.search(message))
    wants_service = bool(_SERVICE_KEYWORDS.search(message))
    wants_live_data = wants_oncall or wants_service
    has_bug_query = bool(mentioned_ids) or bool(exact_results)

    # 4. Auto-extract filters and adaptive top_k from query
    auto_filters, top_k_override = _extract_auto_filters(message)
    merged_filters = {**(auto_filters or {}), **(filters or {})} or None

    # 5. Hybrid retrieval + reranking
    semantic_results: list[dict] = []
    if not wants_live_data or has_bug_query:
        if not exact_results:
            hybrid_results = await hybrid_retrieve(
                session, search_query, top_k=top_k_override, filters=merged_filters,
            )
            semantic_results = rerank(
                search_query, hybrid_results, top_k=top_k_override,
            )
        elif len(exact_bug_ids) < len(mentioned_ids):
            # Some bug IDs weren't found by exact match — try semantic for those
            hybrid_results = await hybrid_retrieve(
                session, search_query, top_k=top_k_override, filters=merged_filters,
            )
            semantic_results = [
                doc for doc in rerank(search_query, hybrid_results, top_k=top_k_override)
                if (doc["source_id"].split(":")[0] if ":" in doc["source_id"] else doc["source_id"])
                not in exact_bug_ids
            ]

    # 6. Build context
    return await _build_context(session, exact_results, semantic_results, wants_oncall, wants_service)


# ---------------------------------------------------------------------------
# Non-streaming chat (backward compatible)
# ---------------------------------------------------------------------------


async def rag_chat(
    session: AsyncSession,
    message: str,
    conversation_history: list[dict] | None = None,
    filters: dict | None = None,
) -> dict:
    """Run a RAG-powered chat: embed query, retrieve context, generate answer."""
    # Check cache
    cached = get_cached_response(message, conversation_history)
    if cached:
        return cached

    context_blocks, sources = await _retrieve(session, message, conversation_history, filters)
    messages = _build_messages(context_blocks, message, conversation_history)

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        max_retries=5,
    )
    response = client.messages.create(
        model=_CHAT_MODEL,
        max_tokens=2048,
        system=_SYSTEM_PROMPT,
        messages=messages,
    )

    answer = response.content[0].text
    result = {"answer": answer, "sources": sources}

    set_cached_response(message, result, conversation_history)
    return result


# ---------------------------------------------------------------------------
# Streaming chat (SSE)
# ---------------------------------------------------------------------------


async def rag_chat_stream(
    session: AsyncSession,
    message: str,
    conversation_history: list[dict] | None = None,
    filters: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Stream a RAG-powered chat response as Server-Sent Events.

    Yields SSE-formatted strings:
      ``event: sources\\ndata: {...}\\n\\n``   — retrieved sources (sent first)
      ``event: token\\ndata: {...}\\n\\n``     — each token chunk
      ``event: done\\ndata: {}\\n\\n``         — completion signal
      ``event: error\\ndata: {...}\\n\\n``     — on error
    """
    try:
        context_blocks, sources = await _retrieve(session, message, conversation_history, filters)

        # Emit sources first so the FE can render source pills immediately
        yield f"event: sources\ndata: {json.dumps({'sources': sources})}\n\n"

        messages = _build_messages(context_blocks, message, conversation_history)

        client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            max_retries=3,
        )
        async with client.messages.stream(
            model=_CHAT_MODEL,
            max_tokens=2048,
            system=_SYSTEM_PROMPT,
            messages=messages,
        ) as stream:
            async for text_chunk in stream.text_stream:
                yield f"event: token\ndata: {json.dumps({'token': text_chunk})}\n\n"

        yield f"event: done\ndata: {{}}\n\n"

    except Exception as e:
        logger.exception("Streaming RAG chat error")
        yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
