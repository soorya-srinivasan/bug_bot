import logging

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.config import settings
from bug_bot.rag.embeddings import embed_query
from bug_bot.rag.vectorstore import similarity_search

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are Bug Bot Assistant, an AI helper for the ShopTech engineering team.
You answer questions about bugs, investigations, and incidents using the context provided below.

Rules:
- Base your answers ONLY on the retrieved context. Do not make up information.
- Always cite the relevant Bug ID (e.g., BUG-001) when referencing specific bugs.
- If the context does not contain enough information to answer, say so clearly.
- Be concise and technical. Engineers are your audience.
- When describing root causes or fixes, include relevant technical details from the context.
- If multiple bugs are relevant, summarize each briefly.
"""

_CHAT_MODEL = "claude-haiku-4-5-20251001"


async def rag_chat(
    session: AsyncSession,
    message: str,
    conversation_history: list[dict] | None = None,
) -> dict:
    """Run a RAG-powered chat: embed query, retrieve context, generate answer."""
    query_embedding = embed_query(message)

    results = await similarity_search(
        session, query_embedding, top_k=settings.rag_top_k
    )

    context_blocks = []
    sources = []
    for doc in results:
        context_blocks.append(
            f"[{doc['source_type'].upper()} — {doc['source_id']}]\n{doc['chunk_text']}"
        )
        sources.append({
            "bug_id": doc["source_id"].split(":")[0] if ":" in doc["source_id"] else doc["source_id"],
            "source_type": doc["source_type"],
            "chunk_text": doc["chunk_text"],
            "similarity": doc["similarity"],
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
