"""Service matcher: identify which registered services a bug report affects."""
import json
import logging

import anthropic

from bug_bot.config import settings
from bug_bot.db.session import async_session
from bug_bot.models.models import ServiceTeamMapping
from sqlalchemy import select

logger = logging.getLogger(__name__)

SERVICE_MATCH_SYSTEM_PROMPT = """\
You are Bug Bot's service matcher.
Given a bug report and a list of registered services, return the canonical service names
that are likely affected. If multiple services could be involved, include all of them.

Respond with a JSON object (no markdown fences):
{"matched_services": ["Service.Name", ...]}. Only pick the services with high confidence rate. Atleast 50%, If you are unable to find the service return [] at this point.
If nothing clearly matches, return {"matched_services": []}.

Rules:
- Match on service name, description, tech stack, or any symptom that implies a specific service.
- Prefer canonical service_name values exactly as listed.
- When in doubt about multiple services, include all plausible ones.
"""


async def _fetch_all_services() -> list[dict]:
    """Fetch all services from the DB."""
    try:
        async with async_session() as session:
            result = await session.execute(
                select(
                    ServiceTeamMapping.service_name,
                    ServiceTeamMapping.description,
                    ServiceTeamMapping.github_repo,
                    ServiceTeamMapping.tech_stack,
                ).order_by(ServiceTeamMapping.service_name)
            )
            return [
                {
                    "service_name": row.service_name,
                    "description": row.description or "",
                    "github_repo": row.github_repo,
                    "tech_stack": row.tech_stack,
                }
                for row in result.all()
            ]
    except Exception:
        logger.exception("Failed to fetch services from DB for matching.")
        return []


def _format_service_list(services: list[dict]) -> str:
    lines = []
    for s in services:
        desc = f": {s['description']}" if s["description"] else ""
        lines.append(f"- {s['service_name']} ({s['tech_stack']}){desc}")
    return "\n".join(lines) if lines else "No services registered."


async def match_services(bug_text: str) -> list[str]:
    """Fetch all services from DB, use Haiku to find which ones match the bug report.

    Returns a list of canonical service_name strings. Falls back to [] on any error.
    """
    if not settings.anthropic_api_key:
        return []

    services = await _fetch_all_services()
    if not services:
        return []

    service_block = _format_service_list(services)
    prompt = (
        f"Bug report:\n{bug_text}\n\n"
        f"Registered services:\n{service_block}"
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=SERVICE_MATCH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        result = json.loads(text)
        matched = result.get("matched_services", [])
        # Validate: only return names that exist in the DB
        known = {s["service_name"] for s in services}
        return [m for m in matched if m in known]
    except Exception:
        logger.exception("Service matching failed; returning empty list.")
        return []
