import logging
import re

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.config import settings
from bug_bot.rag.embeddings import embed_query
from bug_bot.rag.live_context import fetch_oncall_context, fetch_service_mappings_context
from bug_bot.rag.vectorstore import lookup_by_bug_id, similarity_search

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


async def rag_chat(
    session: AsyncSession,
    message: str,
    conversation_history: list[dict] | None = None,
) -> dict:
    """Run a RAG-powered chat: embed query, retrieve context, generate answer."""

    mentioned_ids = list(dict.fromkeys(
        m.upper() for m in re.findall(r"BUG-\d+", message, re.IGNORECASE)
    ))

    exact_results: list[dict] = []
    exact_bug_ids: set[str] = set()
    for bug_id in mentioned_ids:
        docs = await lookup_by_bug_id(session, bug_id)
        if docs:
            exact_results.extend(docs)
            exact_bug_ids.add(bug_id)

    wants_oncall = bool(_ONCALL_KEYWORDS.search(message))
    wants_service = bool(_SERVICE_KEYWORDS.search(message))
    wants_live_data = wants_oncall or wants_service
    has_bug_query = bool(mentioned_ids) or bool(exact_results)

    semantic_results: list[dict] = []
    if not wants_live_data or has_bug_query:
        if not exact_results:
            query_embedding = embed_query(message)
            semantic_results = await similarity_search(
                session, query_embedding, top_k=settings.rag_top_k
            )
        elif len(exact_bug_ids) < len(mentioned_ids):
            query_embedding = embed_query(message)
            semantic_results = [
                doc for doc in await similarity_search(
                    session, query_embedding, top_k=settings.rag_top_k
                )
                if (doc["source_id"].split(":")[0] if ":" in doc["source_id"] else doc["source_id"])
                not in exact_bug_ids
            ]

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
                "similarity": doc["similarity"],
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
                "similarity": doc["similarity"],
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

    context_text = "\n\n---\n\n".join(context_blocks) if context_blocks else "(No relevant documents found)"

    messages = []
    if conversation_history:
        for msg in conversation_history:
            messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({
        "role": "user",
        "content": (
            f"Context from bug database:\n\n{context_text}\n\n---\n\n"
            f"Question: {message}"
        ),
    })

    client = anthropic.Anthropic(
        api_key=settings.anthropic_api_key,
        max_retries=5,
    )
    response = client.messages.create(
        model=_CHAT_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        messages=messages,
    )

    answer = response.content[0].text

    return {
        "answer": answer,
        "sources": sources,
    }
