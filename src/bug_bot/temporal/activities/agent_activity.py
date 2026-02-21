import asyncio
import os
import shutil

import httpx
from temporalio import activity

from bug_bot.config import settings


async def _run_with_heartbeat(coro, heartbeat_secs: float = 30.0):
    """Await *coro* while sending Temporal heartbeats every `heartbeat_secs` seconds.

    Temporal cancels an activity if it doesn't receive a heartbeat within
    `heartbeat_timeout`.  Long-running SDK calls (which can take 10+ minutes)
    must heartbeat to prevent spurious cancellations and retries.
    """
    task = asyncio.ensure_future(coro)
    try:
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=heartbeat_secs)
            except asyncio.TimeoutError:
                activity.heartbeat()
    except BaseException:
        task.cancel()
        raise


async def _download_attachments(bug_id: str, attachments: list[dict]) -> list[dict]:
    """Download Slack private files to the per-bug workspace. Returns list with local_path added."""
    if not attachments:
        return []

    attachments_dir = f"/tmp/bugbot-workspace/{bug_id}/attachments"
    os.makedirs(attachments_dir, exist_ok=True)

    downloaded = []
    async with httpx.AsyncClient() as http:
        for att in attachments:
            url = att.get("url_private")
            name = att.get("name", "attachment")
            if not url:
                downloaded.append(att)
                continue
            try:
                resp = await http.get(
                    url,
                    headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
                    follow_redirects=True,
                    timeout=30.0,
                )
                if resp.status_code == 200:
                    local_path = f"{attachments_dir}/{name}"
                    with open(local_path, "wb") as f:
                        f.write(resp.content)
                    downloaded.append({**att, "local_path": f"./attachments/{name}"})
                    activity.logger.info(f"Downloaded attachment '{name}' for {bug_id}")
                else:
                    activity.logger.warning(
                        f"Failed to download '{name}' for {bug_id}: HTTP {resp.status_code}"
                    )
                    downloaded.append(att)
            except Exception as e:
                activity.logger.warning(f"Error downloading attachment '{name}' for {bug_id}: {e}")
                downloaded.append(att)

    return downloaded


@activity.defn
async def run_agent_investigation(
    bug_id: str,
    description: str,
    severity: str,
    relevant_services: list[str],
    attachments: list[dict] | None = None,
) -> dict:
    """Invoke Claude Agent SDK to investigate the bug."""
    activity.logger.info(
        f"Starting agent investigation for {bug_id} (severity={severity}, "
        f"services={relevant_services}, attachments={len(attachments or [])})"
    )

    if settings.mock_agent:
        activity.logger.info(f"[MOCK] Returning static investigation result for {bug_id}")
        await asyncio.sleep(2)
        return {
            "root_cause": f"Mock root cause for {bug_id}: simulated service degradation",
            "fix_type": "needs_human",
            "action": "escalate",
            "pr_url": None,
            "summary": (
                f"[MOCK] Investigation of `{bug_id}` (severity {severity}). "
                f"Services affected: {', '.join(relevant_services) or 'unknown'}. "
                f"This is a mock response for testing — no real agent was invoked."
            ),
            "confidence": 0.75,
            "recommended_actions": [
                "Check service health dashboards",
                "Review recent deployments",
            ],
            "relevant_services": relevant_services,
            "claude_session_id": "mock-session-id",
            "cost_usd": 0.0,
            "duration_ms": 2000,
        }

    # Download Slack attachments into the per-bug workspace before starting the agent
    if attachments:
        attachments = await _download_attachments(bug_id, attachments)

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

        result = await _run_with_heartbeat(
            run_investigation(
                bug_id=bug_id,
                description=description,
                severity=severity,
                relevant_services=relevant_services,
                attachments=attachments,
            )
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
async def run_continuation_investigation(
    bug_id: str,
    conversation_ids: list[str],
    state: str,
    claude_session_id: str | None = None,
) -> dict:
    """Run a continuation investigation by resuming the original Claude session."""
    activity.logger.info(
        f"Starting continuation investigation for {bug_id} "
        f"(state={state}, new_messages={len(conversation_ids)}, session_id={claude_session_id})"
    )

    if settings.mock_agent:
        activity.logger.info(f"[MOCK] Returning static continuation result for {bug_id} (state={state})")
        await asyncio.sleep(2)
        if state == "awaiting_dev":
            return {
                "root_cause": f"Mock follow-up for {bug_id}",
                "fix_type": "needs_human",
                "action": "post_findings",
                "pr_url": None,
                "summary": (
                    f"[MOCK] Follow-up for `{bug_id}`: Based on the developer's question, "
                    f"the issue appears to be related to the service configuration. "
                    f"This is a mock response for testing."
                ),
                "confidence": 0.8,
                "recommended_actions": ["Review service config", "Check logs"],
                "relevant_services": [],
                "claude_session_id": claude_session_id or "mock-session-id",
                "cost_usd": 0.0,
                "duration_ms": 2000,
            }
        else:
            return {
                "root_cause": f"Mock continuation for {bug_id}",
                "fix_type": "needs_human",
                "action": "escalate",
                "pr_url": None,
                "summary": (
                    f"[MOCK] Continuation for `{bug_id}`: After reviewing the reporter's "
                    f"clarification, the issue requires human investigation. "
                    f"This is a mock response for testing."
                ),
                "confidence": 0.7,
                "recommended_actions": ["Escalate to on-call engineer"],
                "relevant_services": [],
                "claude_session_id": claude_session_id or "mock-session-id",
                "cost_usd": 0.0,
                "duration_ms": 2000,
            }

    try:
        from bug_bot.agent.runner import run_continuation

        result = await _run_with_heartbeat(
            run_continuation(
                bug_id=bug_id,
                conversation_ids=conversation_ids,
                state=state,
                claude_session_id=claude_session_id,
            )
        )
    except BaseException as e:
        activity.logger.error(f"Continuation investigation failed for {bug_id}: {e}")
        return {
            "root_cause": None,
            "fix_type": "needs_human",
            "action": "escalate",
            "pr_url": None,
            "summary": (
                f"Continuation investigation for `{bug_id}` failed: "
                f"{type(e).__name__}: {e}"
            ),
            "confidence": 0.0,
            "recommended_actions": ["Manual investigation required", f"Agent error: {e}"],
            "relevant_services": [],
            "cost_usd": None,
            "duration_ms": 0,
        }

    activity.logger.info(
        f"Continuation complete for {bug_id}: fix_type={result['fix_type']}, "
        f"action={result.get('action')}, confidence={result.get('confidence', 0)}"
    )

    return result


@activity.defn
async def cleanup_workspace(bug_id: str) -> None:
    """Remove the per-bug workspace directory after workflow completion."""
    workspace = f"/tmp/bugbot-workspace/{bug_id}"
    try:
        shutil.rmtree(workspace, ignore_errors=True)
        activity.logger.info(f"Cleaned up workspace for {bug_id}: {workspace}")
    except Exception as e:
        activity.logger.warning(f"Failed to clean workspace for {bug_id}: {e}")
