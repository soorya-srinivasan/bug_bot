"""Slack notifications for on-call assignments."""

import logging
from datetime import date

from slack_sdk.web.async_client import AsyncWebClient

from bug_bot.config import settings

logger = logging.getLogger(__name__)


def _get_slack_client() -> AsyncWebClient:
    """Get Slack client instance."""
    return AsyncWebClient(token=settings.slack_bot_token)


def _slack_configured() -> bool:
    """Check if Slack is properly configured."""
    return bool(settings.slack_bot_token) and not settings.slack_bot_token.startswith("xoxb-your")


async def get_workspace_users() -> list[dict]:
    """Fetch all workspace users via Slack API.
    
    Returns list of user dicts with id, name, real_name, display_name, etc.
    """
    if not _slack_configured():
        return []
    
    client = _get_slack_client()
    try:
        response = await client.users_list()
        if not response.get("ok"):
            return []
        users = response.get("members", [])
        # Filter out bots and deleted users
        return [
            {
                "id": u.get("id"),
                "name": u.get("name"),
                "real_name": u.get("real_name"),
                "display_name": u.get("profile", {}).get("display_name") or u.get("name"),
                "is_bot": u.get("is_bot", False),
                "deleted": u.get("deleted", False),
            }
            for u in users
            if not u.get("is_bot", False) and not u.get("deleted", False)
        ]
    except Exception:
        return []


async def get_user_info(user_id: str) -> dict | None:
    """Get user details by Slack user ID.
    
    Returns dict with id, name, real_name, display_name, etc.
    """
    if not _slack_configured():
        return None
    
    client = _get_slack_client()
    try:
        response = await client.users_info(user=user_id)
        if not response.get("ok"):
            return None
        user = response.get("user", {})
        return {
            "id": user.get("id"),
            "name": user.get("name"),
            "real_name": user.get("real_name"),
            "display_name": user.get("profile", {}).get("display_name") or user.get("name"),
            "is_bot": user.get("is_bot", False),
            "deleted": user.get("deleted", False),
        }
    except Exception:
        return None


async def notify_oncall_assignment(
    engineer_slack_id: str,
    group_name: str,
    start_date: date,
    end_date: date,
    schedule_type: str = "weekly",
    days_of_week: list[int] | None = None,
) -> bool:
    """Send Slack DM/notification to engineer about on-call assignment.
    
    Args:
        engineer_slack_id: Slack user ID of the engineer
        group_name: Name of the service group
        start_date: Start date of the assignment
        end_date: End date of the assignment
        schedule_type: 'weekly' or 'daily'
        days_of_week: For daily schedules, list of day numbers [0-6]
    
    Returns:
        True if notification sent successfully, False otherwise
    """
    if not _slack_configured():
        return False
    
    # Get user info for better message formatting
    user_info = await get_user_info(engineer_slack_id)
    display_name = user_info.get("display_name") if user_info else None
    
    # Format date range
    date_range = f"{start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}"
    
    # Format schedule details
    schedule_details = ""
    if schedule_type == "daily" and days_of_week:
        day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        days = [day_names[d] for d in days_of_week if 0 <= d < 7]
        schedule_details = f" (Days: {', '.join(days)})"
    
    # Build message
    message = (
        f"🔔 *On-Call Assignment*\n\n"
        f"You've been assigned as on-call engineer for *{group_name}*\n"
        f"Period: {date_range}{schedule_details}\n\n"
        f"Please ensure you're available during this time and monitor alerts."
    )
    
    client = _get_slack_client()
    try:
        # Open DM channel with user
        dm_response = await client.conversations_open(users=[engineer_slack_id])
        if not dm_response.get("ok"):
            return False
        
        channel_id = dm_response.get("channel", {}).get("id")
        if not channel_id:
            return False
        
        # Send message
        await client.chat_postMessage(
            channel=channel_id,
            text=message,
        )
        return True
    except Exception:
        return False


async def send_nudge(
    engineer_slack_id: str,
    bug_id: str,
    severity: str,
    original_message: str,
    slack_message_url: str | None = None,
    summary: str | None = None,
) -> str | None:
    """Send a Slack DM nudging an on-call engineer about a bug.

    Returns ``None`` on success, or an error description string on failure.
    """
    if not _slack_configured():
        return "Slack is not configured"

    snippet = (original_message[:200] + "…") if len(original_message) > 200 else original_message

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"👋 Nudge: {bug_id}", "emoji": True},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity:*\n{severity}"},
                {"type": "mrkdwn", "text": f"*Bug ID:*\n{bug_id}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Report:*\n> {snippet}"},
        },
    ]

    if summary:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Investigation summary:*\n{summary}"},
        })

    if slack_message_url:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "View in Slack", "emoji": True},
                    "url": slack_message_url,
                    "action_id": "nudge_view_slack",
                },
            ],
        })

    fallback_text = f"Nudge: {bug_id} ({severity}) — {snippet}"

    client = _get_slack_client()
    try:
        dm_response = await client.conversations_open(users=[engineer_slack_id])
        if not dm_response.get("ok"):
            error = dm_response.get("error", "unknown")
            logger.warning("send_nudge: conversations_open failed for %s: %s", engineer_slack_id, error)
            return f"Could not open DM: {error}"

        channel_id = dm_response.get("channel", {}).get("id")
        if not channel_id:
            logger.warning("send_nudge: no channel_id returned for %s", engineer_slack_id)
            return "Could not resolve DM channel"

        await client.chat_postMessage(
            channel=channel_id,
            text=fallback_text,
            blocks=blocks,
        )
        return None
    except Exception as exc:
        logger.exception("send_nudge: failed for %s on %s", engineer_slack_id, bug_id)
        return str(exc)


async def notify_team_channel_handoff(
    slack_channel_id: str,
    outgoing_id: str,
    incoming_id: str,
    effective_date: date,
) -> bool:
    """Post a handoff announcement to the team channel.

    Args:
        slack_channel_id: Slack channel ID to post in
        outgoing_id: Slack user ID of the outgoing engineer
        incoming_id: Slack user ID of the incoming engineer
        effective_date: Date when the handoff takes effect

    Returns:
        True if message posted successfully, False otherwise
    """
    if not _slack_configured():
        return False

    message = (
        f"\U0001f504 On-call handoff: <@{outgoing_id}> \u2192 <@{incoming_id}> "
        f"effective {effective_date.strftime('%Y-%m-%d')}"
    )

    client = _get_slack_client()
    try:
        await client.chat_postMessage(
            channel=slack_channel_id,
            text=message,
        )
        return True
    except Exception:
        logger.exception(
            "notify_team_channel_handoff: failed for channel %s", slack_channel_id
        )
        return False


async def notify_outgoing_engineer(
    engineer_id: str,
    group_name: str,
    effective_date: date,
    incoming_id: str,
) -> bool:
    """DM the outgoing engineer that their on-call shift is ending.

    Args:
        engineer_id: Slack user ID of the outgoing engineer
        group_name: Name of the service group
        effective_date: Date when the shift ends
        incoming_id: Slack user ID of the incoming engineer

    Returns:
        True if notification sent successfully, False otherwise
    """
    if not _slack_configured():
        return False

    message = (
        f"Your on-call shift for {group_name} ends "
        f"{effective_date.strftime('%Y-%m-%d')}. "
        f"<@{incoming_id}> is taking over."
    )

    client = _get_slack_client()
    try:
        dm_response = await client.conversations_open(users=[engineer_id])
        if not dm_response.get("ok"):
            return False

        channel_id = dm_response.get("channel", {}).get("id")
        if not channel_id:
            return False

        await client.chat_postMessage(
            channel=channel_id,
            text=message,
        )
        return True
    except Exception:
        logger.exception(
            "notify_outgoing_engineer: failed for %s", engineer_id
        )
        return False


async def notify_override_request(
    requested_by_id: str,
    substitute_id: str,
    team_channel_id: str,
    override_date: date,
    reason: str,
) -> bool:
    """Post an override request to the team channel for visibility.

    Args:
        requested_by_id: Slack user ID of the person requesting the override
        substitute_id: Slack user ID of the proposed substitute
        team_channel_id: Slack channel ID to post in
        override_date: Date of the requested override
        reason: Reason for the override request

    Returns:
        True if message posted successfully, False otherwise
    """
    if not _slack_configured():
        return False

    message = (
        f"\U0001f4cb *On-Call Override Request*\n\n"
        f"<@{requested_by_id}> has requested an override for "
        f"*{override_date.strftime('%Y-%m-%d')}*.\n"
        f"Proposed substitute: <@{substitute_id}>\n"
        f"Reason: {reason}\n\n"
        f"A team lead can approve or reject this request."
    )

    client = _get_slack_client()
    try:
        await client.chat_postMessage(
            channel=team_channel_id,
            text=message,
        )
        return True
    except Exception:
        logger.exception(
            "notify_override_request: failed for channel %s", team_channel_id
        )
        return False


async def notify_override_decision(
    requested_by_id: str,
    substitute_id: str,
    decision: str,
    decided_by_id: str,
) -> bool:
    """DM the requester about the outcome of their override request.

    Args:
        requested_by_id: Slack user ID of the person who requested the override
        substitute_id: Slack user ID of the proposed substitute
        decision: 'approved' or 'rejected'
        decided_by_id: Slack user ID of the person who made the decision

    Returns:
        True if notification sent successfully, False otherwise
    """
    if not _slack_configured():
        return False

    status = "approved \u2705" if decision == "approved" else "rejected \u274c"
    message = (
        f"Your on-call override request (substitute: <@{substitute_id}>) "
        f"has been *{status}* by <@{decided_by_id}>."
    )

    client = _get_slack_client()
    try:
        dm_response = await client.conversations_open(users=[requested_by_id])
        if not dm_response.get("ok"):
            return False

        channel_id = dm_response.get("channel", {}).get("id")
        if not channel_id:
            return False

        await client.chat_postMessage(
            channel=channel_id,
            text=message,
        )
        return True
    except Exception:
        logger.exception(
            "notify_override_decision: failed for %s", requested_by_id
        )
        return False


async def notify_oncall_rotation(
    engineer_slack_id: str,
    group_name: str,
    effective_date: date,
    slack_channel_id: str | None = None,
    outgoing_engineer_slack_id: str | None = None,
) -> bool:
    """Send Slack notification about rotation assignment.

    When ``slack_channel_id`` and ``outgoing_engineer_slack_id`` are provided
    the function will also post a handoff message to the team channel and DM
    the outgoing engineer about their shift ending.

    Args:
        engineer_slack_id: Slack user ID of the incoming engineer
        group_name: Name of the service group
        effective_date: Date when rotation takes effect
        slack_channel_id: Optional team channel to announce the handoff in
        outgoing_engineer_slack_id: Optional Slack user ID of the outgoing engineer

    Returns:
        True if the primary DM notification sent successfully, False otherwise
    """
    if not _slack_configured():
        return False

    # Get user info
    user_info = await get_user_info(engineer_slack_id)

    # Build message
    message = (
        f"\U0001f504 *On-Call Rotation*\n\n"
        f"You've been rotated to on-call engineer for *{group_name}*\n"
        f"Effective: {effective_date.strftime('%Y-%m-%d')}\n\n"
        f"Please ensure you're available and monitor alerts."
    )

    client = _get_slack_client()
    try:
        # Open DM channel with user
        dm_response = await client.conversations_open(users=[engineer_slack_id])
        if not dm_response.get("ok"):
            return False

        channel_id = dm_response.get("channel", {}).get("id")
        if not channel_id:
            return False

        # Send message
        await client.chat_postMessage(
            channel=channel_id,
            text=message,
        )

        # If team channel and outgoing engineer are known, send supplementary
        # notifications.  Failures here are logged but do not affect the return
        # value — the primary DM to the incoming engineer already succeeded.
        if slack_channel_id and outgoing_engineer_slack_id:
            await notify_team_channel_handoff(
                slack_channel_id=slack_channel_id,
                outgoing_id=outgoing_engineer_slack_id,
                incoming_id=engineer_slack_id,
                effective_date=effective_date,
            )
            await notify_outgoing_engineer(
                engineer_id=outgoing_engineer_slack_id,
                group_name=group_name,
                effective_date=effective_date,
                incoming_id=engineer_slack_id,
            )

        return True
    except Exception:
        logger.exception(
            "notify_oncall_rotation: failed for %s", engineer_slack_id
        )
        return False
