from temporalio import activity

from bug_bot.config import settings


@activity.defn
async def run_agent_investigation(
    bug_id: str,
    description: str,
    severity: str,
    relevant_services: list[str],
) -> dict:
    """Invoke Claude Agent SDK to investigate the bug."""
    activity.logger.info(
        f"Starting agent investigation for {bug_id} (severity={severity}, "
        f"services={relevant_services})"
    )

    # Check if API key is configured
    # if not settings.anthropic_api_key or settings.anthropic_api_key.startswith("sk-ant-your"):
    #     activity.logger.warning(
    #         f"ANTHROPIC_API_KEY not configured — skipping AI investigation for {bug_id}"
    #     )
    #     return {
    #         "root_cause": None,
    #         "fix_type": "needs_human",
    #         "pr_url": None,
    #         "summary": (
    #             f"Bug `{bug_id}` (severity {severity}) received. "
    #             f"Potentially affects: {', '.join(relevant_services) or 'unknown services'}. "
    #             f"AI investigation skipped — ANTHROPIC_API_KEY not configured."
    #         ),
    #         "confidence": 0.0,
    #         "recommended_actions": ["Configure ANTHROPIC_API_KEY and retry"],
    #         "relevant_services": relevant_services,
    #         "cost_usd": None,
    #         "duration_ms": 0,
    #     }

    try:
        from bug_bot.agent.runner import run_investigation

        result = await run_investigation(
            bug_id=bug_id,
            description=description,
            severity=severity,
            relevant_services=relevant_services,
        )
    except Exception as e:
        activity.logger.error(f"Agent SDK failed for {bug_id}: {e}")
        return {
            "root_cause": None,
            "fix_type": "needs_human",
            "pr_url": None,
            "summary": (
                f"Bug `{bug_id}` (severity {severity}) received. "
                f"Potentially affects: {', '.join(relevant_services) or 'unknown services'}. "
                f"AI investigation failed: {type(e).__name__}: {e}"
            ),
            "confidence": 0.0,
            "recommended_actions": ["Manual investigation required", f"Agent error: {e}"],
            "relevant_services": relevant_services,
            "cost_usd": None,
            "duration_ms": 0,
        }

    activity.logger.info(
        f"Investigation complete for {bug_id}: fix_type={result['fix_type']}, "
        f"confidence={result.get('confidence', 0)}"
    )

    return result
