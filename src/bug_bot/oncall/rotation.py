"""Rotation logic for on-call assignments."""

from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bug_bot.models.models import Team


async def get_rotation_engineers(slack_group_id: str) -> list[str]:
    """Get list of engineers from Slack group for rotation.
    
    Returns list of Slack user IDs in the order they should rotate.
    """
    from bug_bot.slack.user_groups import list_users_in_group
    
    try:
        result = await list_users_in_group(
            usergroup_id=slack_group_id,
            include_disabled=False,
            include_user_details=False,
        )
        return result.get("user_ids", [])
    except Exception:
        return []


def calculate_next_engineer(
    team: "Team",
    rotation_engineers: list[str],
) -> str | None:
    """Calculate next engineer in rotation based on rotation_type.

    Args:
        team: Team with rotation configuration
        rotation_engineers: List of Slack user IDs from Slack group (for round_robin)

    Returns:
        Next engineer Slack user ID, or None if rotation not configured properly
    """
    if not team.rotation_enabled or not team.rotation_type:
        return None

    if team.rotation_type == "round_robin":
        if not rotation_engineers:
            return None
        current_idx = team.current_rotation_index or 0
        # Find current engineer's index in rotation_engineers
        if team.oncall_engineer and team.oncall_engineer in rotation_engineers:
            try:
                current_idx = rotation_engineers.index(team.oncall_engineer)
            except ValueError:
                pass
        next_idx = (current_idx + 1) % len(rotation_engineers)
        return rotation_engineers[next_idx]

    elif team.rotation_type == "custom_order":
        if not team.rotation_order:
            return None
        current_idx = team.current_rotation_index or 0
        next_idx = (current_idx + 1) % len(team.rotation_order)
        return team.rotation_order[next_idx]

    return None


def should_rotate(team: "Team", check_date: date | None = None) -> bool:
    """Check if rotation should occur based on rotation_start_date.

    Rotation happens weekly, starting from rotation_start_date.
    """
    if not team.rotation_enabled or not team.rotation_start_date:
        return False

    if check_date is None:
        check_date = date.today()

    # Check if we've passed the rotation start date
    if check_date < team.rotation_start_date:
        return False

    # Calculate weeks since rotation started
    days_diff = (check_date - team.rotation_start_date).days
    weeks_since_start = days_diff // 7

    # Check if we need to rotate (every week)
    if team.current_rotation_index is None:
        # First rotation
        return True

    # Rotate if we've moved to a new week
    expected_index = weeks_since_start % (len(team.rotation_order) if team.rotation_order else 1)
    return team.current_rotation_index != expected_index


async def apply_rotation(
    team: "Team",
    new_engineer: str,
    effective_date: date | None = None,
) -> dict:
    """Apply rotation and return update data for Team.

    Returns dict with fields to update in Team.
    """
    if effective_date is None:
        effective_date = date.today()

    # Calculate new rotation index
    if team.rotation_type == "round_robin":
        engineers = await get_rotation_engineers(team.slack_group_id)
        if new_engineer in engineers:
            new_index = engineers.index(new_engineer)
        else:
            new_index = 0
    elif team.rotation_type == "custom_order" and team.rotation_order:
        if new_engineer in team.rotation_order:
            new_index = team.rotation_order.index(new_engineer)
        else:
            new_index = 0
    else:
        new_index = 0

    return {
        "oncall_engineer": new_engineer,
        "current_rotation_index": new_index,
        "updated_at": None,  # Will be set by repository
    }
