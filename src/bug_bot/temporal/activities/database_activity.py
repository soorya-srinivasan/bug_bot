from temporalio import activity

from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository


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
