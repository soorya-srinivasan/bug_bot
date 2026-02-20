"""Triage gate: quick severity + routing classification before full investigation."""

import json
import logging

import anthropic

from bug_bot.config import settings

logger = logging.getLogger(__name__)

_TRIAGE_SYSTEM_PROMPT_TEMPLATE = """\
You are Bug Bot's triage classifier for ShopTech.
Given a bug report from Slack, respond with a JSON object (no markdown fences) containing:
- severity: P1 | P2 | P3 | P4
- category: one of [api_error, ui_bug, data_issue, performance, security, infrastructure, unknown]
- affected_services: list of canonical service_name values from the registry below that are likely affected
- summary: one-sentence plain-English summary of the bug
- needs_investigation: boolean, true unless the report is clearly spam/noise

Severity guide:
  P1 - Production is down or money-losing; needs immediate action
  P2 - Major feature broken for many users
  P3 - Bug with workaround available
  P4 - Cosmetic / minor issue

[SERVICE REGISTRY]
{service_registry}
"""


async def triage_bug_report(message_text: str, reporter_user_id: str) -> dict:
    """Run a fast Claude call to classify a bug report.

    Returns a dict with keys: severity, category, affected_services, summary,
    needs_investigation.  Falls back to safe defaults on any error.
    """
    defaults = {
        "severity": "P3",
        "category": "unknown",
        "affected_services": [],
        "summary": message_text[:120],
        "needs_investigation": True,
    }

    if not settings.anthropic_api_key:
        logger.warning("No Anthropic API key configured; returning default triage.")
        return defaults

    try:
        from bug_bot.service_matcher import _fetch_all_services, _format_service_list
        services = await _fetch_all_services()
        service_registry = _format_service_list(services) if services else "No services registered."
        system_prompt = _TRIAGE_SYSTEM_PROMPT_TEMPLATE.format(service_registry=service_registry)
    except Exception:
        logger.exception("Failed to load service registry for triage; using empty list.")
        system_prompt = _TRIAGE_SYSTEM_PROMPT_TEMPLATE.format(service_registry="No services registered.")

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=300,
            system=system_prompt,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Bug report from <@{reporter_user_id}>:\n\n{message_text}"
                    ),
                }
            ],
        )
        text = response.content[0].text.strip()
        # Strip markdown code fences that the model sometimes adds despite instructions
        if text.startswith("```"):
            text = text.split("```", 2)[1]          # drop opening fence line
            if text.startswith("json"):
                text = text[4:]                      # drop "json" language tag
            text = text.rsplit("```", 1)[0].strip()  # drop closing fence
        result = json.loads(text)
        # Ensure all expected keys are present
        for key, default_val in defaults.items():
            result.setdefault(key, default_val)
        return result
    except Exception:
        logger.exception("Triage classification failed; using defaults.")
        return defaults
