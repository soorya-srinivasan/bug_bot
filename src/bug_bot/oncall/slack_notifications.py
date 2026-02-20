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


async def notify_oncall_rotation(
    engineer_slack_id: str,
    group_name: str,
    effective_date: date,
) -> bool:
    """Send Slack notification about rotation assignment.
    
    Args:
        engineer_slack_id: Slack user ID of the engineer
        group_name: Name of the service group
        effective_date: Date when rotation takes effect
    
    Returns:
        True if notification sent successfully, False otherwise
    """
    if not _slack_configured():
        return False
    
    # Get user info
    user_info = await get_user_info(engineer_slack_id)
    
    # Build message
    message = (
        f"🔄 *On-Call Rotation*\n\n"
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
        return True
    except Exception:
        return False
