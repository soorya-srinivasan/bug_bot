import logging

import anthropic

from bug_bot.config import settings

logger = logging.getLogger(__name__)

_REWRITE_PROMPT = """\
Given the conversation history and the user's latest message, rewrite the latest message \
as a standalone search query that captures the full intent. If the message is already \
standalone, return it unchanged. Return ONLY the rewritten query, nothing else.

Conversation history:
{history}

Latest message: {message}

Rewritten query:"""


async def rewrite_query(
    message: str,
    conversation_history: list[dict] | None = None,
) -> str:
    """Rewrite a follow-up message into a standalone query using conversation context.

    Only calls the LLM when there is actual conversation history to resolve.
    For the first message in a conversation, returns it unchanged.
    """
    if not conversation_history:
        return message

    history_text = "\n".join(
        f"{msg['role'].capitalize()}: {msg['content'][:200]}"
        for msg in conversation_history[-6:]  # last 3 turns
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": _REWRITE_PROMPT.format(history=history_text, message=message),
            }],
        )
        rewritten = response.content[0].text.strip()
        if rewritten:
            logger.debug("Query rewritten: %r -> %r", message, rewritten)
            return rewritten
    except Exception:
        logger.warning("Query rewrite failed, using original query", exc_info=True)

    return message
