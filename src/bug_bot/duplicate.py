"""Duplicate detection: compare a new bug report against recent open bugs."""
import json
import logging

import anthropic

from bug_bot.config import settings

logger = logging.getLogger(__name__)

DUPLICATE_SYSTEM_PROMPT = """\
You are Bug Bot's duplicate detector for ShopTech.
Given a new bug report and a list of recent open bugs, determine whether the new
report describes the same root issue as any existing bug.

Respond with a JSON object (no markdown fences):
{
  "is_duplicate": true | false,
  "duplicate_of": "<bug_id>" | null,
  "confidence": 0.0-1.0
}

Rules:
- Only flag as duplicate if the core symptom and affected service(s) match.
- Ignore superficial wording differences; focus on what is broken and where.
- If no confident match, return is_duplicate: false.
"""


async def check_duplicate_bug(
    new_message: str,
    triage_summary: str,
    recent_bugs: list[dict],  # list of {"bug_id": str, "message": str}
) -> dict | None:
    """Return {"bug_id", "confidence"} if a duplicate is found, else None."""
    if not recent_bugs or not settings.anthropic_api_key:
        return None

    bug_list_text = "\n".join(
        f"- {b['bug_id']}: {b['message'][:300]}" for b in recent_bugs
    )
    prompt = (
        f"New report (triage summary: {triage_summary}):\n{new_message}\n\n"
        f"Recent open bugs:\n{bug_list_text}"
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",  # cheapest model; this is a quick check
            max_tokens=150,
            system=DUPLICATE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        result = json.loads(text)
        if result.get("is_duplicate") and result.get("duplicate_of"):
            return {
                "bug_id": result["duplicate_of"],
                "confidence": result.get("confidence", 0.0),
            }
    except Exception:
        logger.exception("Duplicate check failed; treating as non-duplicate.")
    return None
