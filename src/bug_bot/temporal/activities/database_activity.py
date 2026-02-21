from datetime import datetime, timedelta, timezone

from temporalio import activity

from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository
from bug_bot.oncall import service as oncall_service


@activity.defn
async def update_bug_assignee(bug_id: str, user_id: str) -> None:
    """Set the assignee (dev who took over) for a bug report."""
    async with async_session() as session:
        await BugRepository(session).update_assignee(bug_id, user_id)
    activity.logger.info(f"Assignee for {bug_id} set to {user_id}")


@activity.defn
async def update_bug_status(bug_id: str, status: str) -> None:
    """Update bug report status in the application database."""
    async with async_session() as session:
        repo = BugRepository(session)
        await repo.update_status(bug_id, status)
    activity.logger.info(f"Bug {bug_id} status updated to: {status}")


@activity.defn
async def save_investigation_result(bug_id: str, result: dict) -> None:
    """Save investigation results to the database."""
    async with async_session() as session:
        repo = BugRepository(session)
        await repo.save_investigation(bug_id, result)
    activity.logger.info(f"Investigation saved for bug {bug_id}")


@activity.defn
async def store_summary_thread_ts(bug_id: str, summary_thread_ts: str) -> None:
    """Store the summary thread timestamp for a bug report."""
    async with async_session() as session:
        repo = BugRepository(session)
        await repo.store_summary_thread_ts(bug_id, summary_thread_ts)
    activity.logger.info(f"Summary thread_ts stored for bug {bug_id}: {summary_thread_ts}")


@activity.defn
async def get_sla_config_for_severity(severity: str) -> dict | None:
    """Fetch SLA configuration for a given severity level."""
    async with async_session() as session:
        repo = BugRepository(session)
        config = await repo.get_sla_config(severity)
        if config is None:
            return None
        return {
            "severity": config.severity,
            "acknowledgement_target_min": config.acknowledgement_target_min,
            "resolution_target_min": config.resolution_target_min,
            "follow_up_interval_min": config.follow_up_interval_min,
            "escalation_threshold": config.escalation_threshold,
            "escalation_contacts": config.escalation_contacts,
        }


@activity.defn
async def fetch_oncall_for_services(service_names: list[str]) -> list[dict]:
    """Return on-call info (oncall_engineer, service_owner, slack_group_id) for the given service names."""
    activity.logger.info(f"Fetching on-call for services: {service_names}")
    async with async_session() as session:
        repo = BugRepository(session)
        entries = await repo.get_oncall_for_services(service_names)
        activity.logger.info(f"Found {len(entries)} on-call entries: {entries}")
        return entries


@activity.defn
async def save_followup_result(bug_id: str, trigger_state: str, result: dict) -> None:
    """Save follow-up investigation results to the database."""
    async with async_session() as session:
        repo = BugRepository(session)
        await repo.save_followup_investigation(bug_id, trigger_state, result)
    activity.logger.info(f"Follow-up investigation saved for {bug_id} (state={trigger_state})")


@activity.defn
async def log_conversation_event(
    bug_id: str,
    message_type: str,
    sender_type: str,
    sender_id: str | None = None,
    channel: str | None = None,
    message_text: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Append an event to the bug_conversations audit trail."""
    async with async_session() as session:
        repo = BugRepository(session)
        await repo.log_conversation(
            bug_id=bug_id, message_type=message_type, sender_type=sender_type,
            sender_id=sender_id, channel=channel, message_text=message_text, metadata=metadata,
        )
    activity.logger.info(f"Conversation event logged for {bug_id}: type={message_type}")


@activity.defn
async def find_stale_bugs(inactivity_days: int) -> list[dict]:
    """Return open bugs with no human interaction in the last inactivity_days days."""
    threshold = datetime.now(timezone.utc) - timedelta(days=inactivity_days)
    async with async_session() as session:
        bugs = await BugRepository(session).get_stale_open_bugs(threshold)
    activity.logger.info(f"Found {len(bugs)} stale bugs (threshold={threshold.date()})")
    return [
        {"bug_id": b.bug_id, "temporal_workflow_id": b.temporal_workflow_id, "status": b.status}
        for b in bugs
    ]


@activity.defn
async def mark_bug_auto_closed(bug_id: str) -> None:
    """Resolve a bug directly and log the auto-close event (used when workflow is not running)."""
    async with async_session() as session:
        repo = BugRepository(session)
        await repo.update_status(bug_id, "resolved")
        await repo.log_conversation(
            bug_id=bug_id,
            message_type="resolved",
            sender_type="system",
            sender_id=None,
            channel=None,
            message_text="Auto-closed due to inactivity",
            metadata={"reason": "auto_close_inactivity"},
        )
    activity.logger.info(f"Bug {bug_id} auto-closed (direct path)")


@activity.defn
async def fetch_rotation_enabled_teams() -> list[dict]:
    """Return lightweight dicts for all rotation-enabled teams."""
    async with async_session() as session:
        teams = await BugRepository(session).get_rotation_enabled_teams()
    activity.logger.info(f"Found {len(teams)} rotation-enabled teams")
    return [{"id": str(t.id), "slack_group_id": t.slack_group_id} for t in teams]


@activity.defn
async def process_team_rotation(team_id: str) -> dict:
    """Run auto-rotation for a single team, catching errors so one failure doesn't block others."""
    try:
        async with async_session() as session:
            repo = BugRepository(session)
            rotated = await oncall_service.process_auto_rotation(repo, team_id)
        return {"team_id": team_id, "rotated": rotated}
    except Exception as e:
        activity.logger.error(f"Rotation failed for team {team_id}: {e}")
        return {"team_id": team_id, "rotated": False, "error": str(e)}
