"""Service layer for on-call management."""

from datetime import date, timedelta
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
    origin: str = "manual",
    send_notification: bool = True,
) -> "OnCallSchedule":
    """Assign on-call engineer for a period.

    Creates schedule, logs history, and optionally sends notification.
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
            "origin": origin,
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
            team_name = team.name or team.slack_group_id
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
    """
    if check_date is None:
        check_date = date.today()

    # Check active override or schedule
    current = await repo.get_current_oncall_for_team(team_id, check_date=check_date)
    if current and current.get("source") in ("schedule", "override"):
        return current

    # Check rotation if enabled
    team = await repo.get_team_by_id(team_id)
    if team and team.rotation_enabled:
        if rotation.should_rotate(team, check_date):
            # Get eligible members for rotation
            memberships = await repo.get_eligible_members_for_rotation(team_id)
            eligible_ids = [m.slack_user_id for m in memberships] if memberships else None

            rotation_engineers = await rotation.get_rotation_engineers(
                team.slack_group_id, eligible_member_ids=eligible_ids
            )

            if team.rotation_type == "weighted" and memberships:
                shift_counts = await repo.get_shift_counts_for_team(team_id)
                membership_dicts = [
                    {"slack_user_id": m.slack_user_id, "weight": m.weight, "is_eligible_for_oncall": m.is_eligible_for_oncall}
                    for m in memberships
                ]
                next_engineer = rotation.calculate_next_engineer(
                    team, rotation_engineers,
                    memberships=membership_dicts,
                    shift_counts=shift_counts,
                )
            else:
                next_engineer = rotation.calculate_next_engineer(
                    team, rotation_engineers,
                    eligible_member_ids=eligible_ids,
                )

            if next_engineer:
                update_data = await rotation.apply_rotation(team, next_engineer, check_date)
                await repo.update_team(team_id, update_data)

                await repo.log_oncall_change(
                    team_id=team_id,
                    engineer_slack_id=next_engineer,
                    change_type="auto_rotation",
                    effective_date=check_date,
                    previous_engineer_slack_id=team.oncall_engineer,
                    change_reason=f"Automatic rotation ({team.rotation_type})",
                )

                team_name = team.name or team.slack_group_id
                await slack_notifications.notify_oncall_rotation(
                    engineer_slack_id=next_engineer,
                    group_name=team_name,
                    effective_date=check_date,
                    slack_channel_id=team.slack_channel_id,
                    outgoing_engineer_slack_id=team.oncall_engineer,
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
    if not team or not team.rotation_enabled or not team.is_active:
        return False

    if not rotation.should_rotate(team, check_date):
        return False

    # Check idempotency: see if rotation was already applied today
    from bug_bot.models.models import OnCallHistory
    from sqlalchemy import select, func
    from bug_bot.db.session import async_session
    # Use the existing repo session to check
    existing = await repo.get_oncall_history(team_id, page=1, page_size=1)
    if existing[0]:
        last_entry = existing[0][0]
        if (last_entry.change_type == "auto_rotation"
                and last_entry.effective_date == check_date):
            return False

    # Get eligible members
    memberships = await repo.get_eligible_members_for_rotation(team_id)
    eligible_ids = [m.slack_user_id for m in memberships] if memberships else None

    rotation_engineers = await rotation.get_rotation_engineers(
        team.slack_group_id, eligible_member_ids=eligible_ids
    )

    if team.rotation_type == "weighted" and memberships:
        shift_counts = await repo.get_shift_counts_for_team(team_id)
        membership_dicts = [
            {"slack_user_id": m.slack_user_id, "weight": m.weight, "is_eligible_for_oncall": m.is_eligible_for_oncall}
            for m in memberships
        ]
        next_engineer = rotation.calculate_next_engineer(
            team, rotation_engineers,
            memberships=membership_dicts,
            shift_counts=shift_counts,
        )
    elif team.rotation_type == "round_robin":
        next_engineer = rotation.calculate_next_engineer(
            team, rotation_engineers,
            eligible_member_ids=eligible_ids,
        )
    elif team.rotation_type == "custom_order":
        next_engineer = rotation.calculate_next_engineer(team, [])
    else:
        return False

    if not next_engineer:
        return False

    # Apply rotation
    update_data = await rotation.apply_rotation(team, next_engineer, check_date)
    await repo.update_team(team_id, update_data)

    # Log history (dual-writes to both oncall_history and oncall_audit_logs)
    await repo.log_oncall_change(
        team_id=team_id,
        engineer_slack_id=next_engineer,
        change_type="auto_rotation",
        effective_date=check_date,
        previous_engineer_slack_id=team.oncall_engineer,
        change_reason=f"Automatic rotation ({team.rotation_type})",
    )

    # Send notification
    team_name = team.name or team.slack_group_id
    await slack_notifications.notify_oncall_rotation(
        engineer_slack_id=next_engineer,
        group_name=team_name,
        effective_date=check_date,
        slack_channel_id=team.slack_channel_id,
        outgoing_engineer_slack_id=team.oncall_engineer,
    )

    # Generate lookahead schedules
    await _generate_and_persist_lookahead(repo, team, rotation_engineers, memberships)

    return True


async def _generate_and_persist_lookahead(
    repo: BugRepository,
    team: "Team",
    rotation_engineers: list[str],
    memberships: list | None = None,
    weeks: int = 4,
) -> None:
    """Delete future auto schedules and regenerate lookahead."""
    await repo.delete_future_auto_schedules(str(team.id))

    shift_counts = await repo.get_shift_counts_for_team(str(team.id))
    membership_dicts = None
    if memberships:
        membership_dicts = [
            {"slack_user_id": m.slack_user_id, "weight": m.weight, "is_eligible_for_oncall": m.is_eligible_for_oncall}
            for m in memberships
        ]

    lookahead = rotation.generate_schedule_lookahead(
        team, rotation_engineers, weeks=weeks,
        memberships=membership_dicts,
        shift_counts=shift_counts,
    )

    for entry in lookahead:
        try:
            await repo.create_oncall_schedule(
                team_id=str(team.id),
                data={
                    "engineer_slack_id": entry["engineer_slack_id"],
                    "start_date": entry["start_date"],
                    "end_date": entry["end_date"],
                    "schedule_type": "weekly",
                    "created_by": "SYSTEM",
                    "origin": "auto",
                },
            )
        except Exception:
            pass  # Overlap with manual schedule is OK, skip


async def preview_rotation(
    repo: BugRepository,
    team_id: str,
    weeks: int = 4,
) -> list[dict]:
    """Simulate rotation for the next N weeks without persisting.

    Returns list of {week_number, start_date, end_date, engineer_slack_id}.
    """
    team = await repo.get_team_by_id(team_id)
    if not team:
        return []

    memberships = await repo.get_eligible_members_for_rotation(team_id)
    eligible_ids = [m.slack_user_id for m in memberships] if memberships else None
    rotation_engineers = await rotation.get_rotation_engineers(
        team.slack_group_id, eligible_member_ids=eligible_ids
    )

    shift_counts = await repo.get_shift_counts_for_team(team_id)
    membership_dicts = None
    if memberships:
        membership_dicts = [
            {"slack_user_id": m.slack_user_id, "weight": m.weight, "is_eligible_for_oncall": m.is_eligible_for_oncall}
            for m in memberships
        ]

    return rotation.generate_schedule_lookahead(
        team, rotation_engineers, weeks=weeks,
        memberships=membership_dicts,
        shift_counts=shift_counts,
    )


async def generate_schedules(
    repo: BugRepository,
    team_id: str,
    weeks: int = 4,
) -> list[dict]:
    """Force-generate auto schedules for next N weeks."""
    team = await repo.get_team_by_id(team_id)
    if not team:
        return []

    memberships = await repo.get_eligible_members_for_rotation(team_id)
    eligible_ids = [m.slack_user_id for m in memberships] if memberships else None
    rotation_engineers = await rotation.get_rotation_engineers(
        team.slack_group_id, eligible_member_ids=eligible_ids
    )

    await _generate_and_persist_lookahead(repo, team, rotation_engineers, memberships, weeks)

    # Return the generated preview
    return await preview_rotation(repo, team_id, weeks)


async def approve_override(
    repo: BugRepository,
    override_id: str,
    approved_by: str,
) -> "OnCallOverride | None":
    """Approve a pending override."""
    from bug_bot.models.models import OnCallOverride
    override = await repo.get_oncall_override_by_id(override_id)
    if not override or override.status != "pending":
        return None
    updated = await repo.update_oncall_override(override_id, {
        "status": "approved",
        "approved_by": approved_by,
    })
    if updated:
        team = await repo.get_team_by_id(str(updated.team_id))
        await slack_notifications.notify_override_decision(
            requested_by_id=updated.requested_by or updated.created_by,
            substitute_id=updated.substitute_engineer_slack_id,
            decision="approved",
            decided_by_id=approved_by,
        )
    return updated


async def reject_override(
    repo: BugRepository,
    override_id: str,
    rejected_by: str,
) -> "OnCallOverride | None":
    """Reject a pending override."""
    override = await repo.get_oncall_override_by_id(override_id)
    if not override or override.status != "pending":
        return None
    updated = await repo.update_oncall_override(override_id, {
        "status": "rejected",
        "approved_by": rejected_by,
    })
    if updated:
        await slack_notifications.notify_override_decision(
            requested_by_id=updated.requested_by or updated.created_by,
            substitute_id=updated.substitute_engineer_slack_id,
            decision="rejected",
            decided_by_id=rejected_by,
        )
    return updated
