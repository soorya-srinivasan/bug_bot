"""Service layer for on-call management."""

from datetime import date
from typing import TYPE_CHECKING

from bug_bot.db.repository import BugRepository
from bug_bot.oncall import rotation, slack_notifications

if TYPE_CHECKING:
    from bug_bot.models.models import Team, OnCallSchedule


async def assign_oncall(
    repo: BugRepository,
    team_id: str,
    engineer_slack_id: str,
    start_date: date,
    end_date: date,
    schedule_type: str,
    created_by: str,
    days_of_week: list[int] | None = None,
    send_notification: bool = True,
) -> "OnCallSchedule":
    """Assign on-call engineer for a period.

    Creates schedule, logs history, and optionally sends notification.

    Args:
        repo: BugRepository instance
        team_id: Team ID
        engineer_slack_id: Slack user ID of engineer
        start_date: Start date of assignment
        end_date: End date of assignment
        schedule_type: 'weekly' or 'daily'
        created_by: Slack user ID who created the assignment
        days_of_week: For daily schedules, list of day numbers [0-6]
        send_notification: Whether to send Slack notification

    Returns:
        Created OnCallSchedule instance
    """
    # Check for overlapping schedules
    has_overlap = await repo.check_schedule_overlap(team_id, start_date, end_date)
    if has_overlap:
        raise ValueError(f"Schedule overlaps with existing schedule for team {team_id}")

    # Get current on-call before creating schedule
    current = await repo.get_current_oncall_for_team(team_id, check_date=start_date)
    previous_engineer = current.get("engineer_slack_id") if current else None

    # Create schedule
    schedule = await repo.create_oncall_schedule(
        team_id=team_id,
        data={
            "engineer_slack_id": engineer_slack_id,
            "start_date": start_date,
            "end_date": end_date,
            "schedule_type": schedule_type,
            "days_of_week": days_of_week,
            "created_by": created_by,
        },
    )

    # Log history
    await repo.log_oncall_change(
        team_id=team_id,
        engineer_slack_id=engineer_slack_id,
        change_type="schedule_created",
        effective_date=start_date,
        previous_engineer_slack_id=previous_engineer,
        changed_by=created_by,
        change_reason=f"Schedule created: {schedule_type} from {start_date} to {end_date}",
    )

    # Send notification if requested and schedule starts today or earlier
    if send_notification and start_date <= date.today():
        team = await repo.get_team_by_id(team_id)
        if team:
            team_name = team.slack_group_id
            await slack_notifications.notify_oncall_assignment(
                engineer_slack_id=engineer_slack_id,
                group_name=team_name,
                start_date=start_date,
                end_date=end_date,
                schedule_type=schedule_type,
                days_of_week=days_of_week,
            )

    return schedule


async def get_current_oncall(
    repo: BugRepository,
    team_id: str,
    check_date: date | None = None,
) -> dict | None:
    """Get current on-call engineer for a team with fallback logic.

    Checks schedule first, then rotation, then manual assignment.

    Returns dict with engineer_slack_id, effective_date, source, schedule_id.
    """
    if check_date is None:
        check_date = date.today()

    # Check active schedule
    current = await repo.get_current_oncall_for_team(team_id, check_date=check_date)
    if current and current.get("source") == "schedule":
        return current

    # Check rotation if enabled
    team = await repo.get_team_by_id(team_id)
    if team and team.rotation_enabled:
        # Check if rotation should occur
        if rotation.should_rotate(team, check_date):
            # Apply rotation
            rotation_engineers = await rotation.get_rotation_engineers(team.slack_group_id)
            if team.rotation_type == "round_robin":
                next_engineer = rotation.calculate_next_engineer(team, rotation_engineers)
            elif team.rotation_type == "custom_order":
                next_engineer = rotation.calculate_next_engineer(team, rotation_engineers)
            else:
                next_engineer = None

            if next_engineer:
                # Apply rotation
                update_data = await rotation.apply_rotation(team, next_engineer, check_date)
                await repo.update_team(team_id, update_data)

                # Log history
                await repo.log_oncall_change(
                    team_id=team_id,
                    engineer_slack_id=next_engineer,
                    change_type="auto_rotation",
                    effective_date=check_date,
                    previous_engineer_slack_id=team.oncall_engineer,
                    change_reason=f"Automatic rotation ({team.rotation_type})",
                )

                # Send notification
                team_name = team.slack_group_id
                await slack_notifications.notify_oncall_rotation(
                    engineer_slack_id=next_engineer,
                    group_name=team_name,
                    effective_date=check_date,
                )

                return {
                    "engineer_slack_id": next_engineer,
                    "effective_date": check_date,
                    "source": "rotation",
                    "schedule_id": None,
                }

    # Fallback to manual assignment
    if current and current.get("source") == "manual":
        return current

    return None


async def process_auto_rotation(
    repo: BugRepository,
    team_id: str,
    check_date: date | None = None,
) -> bool:
    """Check if rotation is needed and apply it.

    Returns True if rotation was applied, False otherwise.
    """
    if check_date is None:
        check_date = date.today()

    team = await repo.get_team_by_id(team_id)
    if not team or not team.rotation_enabled:
        return False

    if not rotation.should_rotate(team, check_date):
        return False

    # Get rotation engineers
    rotation_engineers = await rotation.get_rotation_engineers(team.slack_group_id)
    if team.rotation_type == "round_robin":
        next_engineer = rotation.calculate_next_engineer(team, rotation_engineers)
    elif team.rotation_type == "custom_order":
        next_engineer = rotation.calculate_next_engineer(team, [])
    else:
        return False

    if not next_engineer:
        return False

    # Apply rotation
    update_data = await rotation.apply_rotation(team, next_engineer, check_date)
    await repo.update_team(team_id, update_data)

    # Log history
    await repo.log_oncall_change(
        team_id=team_id,
        engineer_slack_id=next_engineer,
        change_type="auto_rotation",
        effective_date=check_date,
        previous_engineer_slack_id=team.oncall_engineer,
        change_reason=f"Automatic rotation ({team.rotation_type})",
    )

    # Send notification
    team_name = team.slack_group_id
    await slack_notifications.notify_oncall_rotation(
        engineer_slack_id=next_engineer,
        group_name=team_name,
        effective_date=check_date,
    )

    return True
