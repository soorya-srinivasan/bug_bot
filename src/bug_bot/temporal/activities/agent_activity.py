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
        activity.logger.info(
            f"Investigation complete for {bug_id}: fix_type={result['fix_type']}, "
            f"confidence={result.get('confidence', 0)}"
        )
        return result
    except BaseExceptionGroup as eg:
        # Unwrap the asyncio TaskGroup wrapper to get the real error
        inner = eg.exceptions[0] if eg.exceptions else eg
        activity.logger.error(
            f"Agent SDK failed for {bug_id}: {type(inner).__name__}: {inner}",
            exc_info=True,
        )
        err_msg = f"{type(inner).__name__}: {inner}"
    except BaseException as e:
        # Catch CancelledError and other BaseException subclasses that bypass except Exception.
        # CancelledError (Python 3.8+) is BaseException, not Exception — if it leaks from the
        # SDK thread it must be caught here rather than letting Temporal see it as a cancellation.
        activity.logger.error(
            f"Agent SDK failed for {bug_id}: {type(e).__name__}: {e}",
            exc_info=True,
        )
        err_msg = f"{type(e).__name__}: {e}"

    return {
        "root_cause": None,
        "fix_type": "needs_human",
        "pr_url": None,
        "summary": (
            f"Bug `{bug_id}` (severity {severity}) received. "
            f"Potentially affects: {', '.join(relevant_services) or 'unknown services'}. "
            f"AI investigation failed: {err_msg}"
        ),
        "confidence": 0.0,
        "recommended_actions": ["Manual investigation required", f"Agent error: {err_msg}"],
        "relevant_services": relevant_services,
        "cost_usd": None,
        "duration_ms": 0,
    }


@activity.defn
async def run_followup_investigation(
    bug_id: str,
    dev_message: str,
    reply_type: str,
    claude_session_id: str | None = None,
) -> dict:
    """Run a follow-up investigation by resuming the original Claude session."""
    activity.logger.info(
        f"Starting follow-up investigation for {bug_id} "
        f"(reply_type={reply_type}, session_id={claude_session_id})"
    )

    try:
        from bug_bot.agent.runner import run_followup

        result = await run_followup(
            bug_id=bug_id,
            dev_message=dev_message,
            reply_type=reply_type,
            claude_session_id=claude_session_id,
        )
    except BaseException as e:
        activity.logger.error(f"Follow-up investigation failed for {bug_id}: {e}")
        return {
            "root_cause": None,
            "fix_type": "needs_human",
            "pr_url": None,
            "summary": (
                f"Follow-up investigation for `{bug_id}` failed: "
                f"{type(e).__name__}: {e}"
            ),
            "confidence": 0.0,
            "recommended_actions": ["Manual investigation required", f"Agent error: {e}"],
            "relevant_services": [],
            "cost_usd": None,
            "duration_ms": 0,
        }

    activity.logger.info(
        f"Follow-up complete for {bug_id}: fix_type={result['fix_type']}, "
        f"confidence={result.get('confidence', 0)}"
    )

    return result
