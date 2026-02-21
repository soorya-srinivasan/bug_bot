"""Rotation logic for on-call assignments."""

from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bug_bot.models.models import Team


async def get_rotation_engineers(
    slack_group_id: str,
    eligible_member_ids: list[str] | None = None,
) -> list[str]:
    """Get list of engineers from Slack group for rotation.

    Args:
        slack_group_id: Slack user group ID to fetch members from.
        eligible_member_ids: If provided, only return engineers whose Slack
            user IDs appear in this list. Engineers not in the list are
            filtered out while preserving the original ordering.

    Returns:
        List of Slack user IDs in the order they should rotate.
    """
    from bug_bot.slack.user_groups import list_users_in_group

    try:
        result = await list_users_in_group(
            usergroup_id=slack_group_id,
            include_disabled=False,
            include_user_details=False,
        )
        user_ids: list[str] = result.get("user_ids", [])
    except Exception:
        return []

    if eligible_member_ids is not None:
        eligible_set = set(eligible_member_ids)
        user_ids = [uid for uid in user_ids if uid in eligible_set]

    return user_ids


def calculate_next_engineer(
    team: "Team",
    rotation_engineers: list[str],
    *,
    eligible_member_ids: list[str] | None = None,
    memberships: list[dict] | None = None,
    shift_counts: dict[str, int] | None = None,
) -> str | None:
    """Calculate next engineer in rotation based on rotation_type.

    Args:
        team: Team with rotation configuration.
        rotation_engineers: List of Slack user IDs from Slack group
            (used for round_robin and as a fallback).
        eligible_member_ids: Optional filter applied to *rotation_engineers*
            for the round_robin strategy. If provided, only engineers in
            this list are considered.
        memberships: List of TeamMembership-like dicts, each containing at
            least ``slack_user_id``, ``weight`` (float), and
            ``is_eligible_for_oncall`` (bool). Required for the 'weighted'
            strategy.
        shift_counts: Mapping of slack_user_id -> number of shifts already
            completed. Used by the 'weighted' strategy to compute actual
            ratios. Defaults to an empty dict (first run).

    Returns:
        Next engineer Slack user ID, or None if rotation is not configured
        properly or no eligible engineers exist.
    """
    if not team.rotation_enabled or not team.rotation_type:
        return None

    if team.rotation_type == "round_robin":
        engineers = list(rotation_engineers)
        if eligible_member_ids is not None:
            eligible_set = set(eligible_member_ids)
            engineers = [e for e in engineers if e in eligible_set]
        if not engineers:
            return None

        current_idx = team.current_rotation_index or 0
        if team.oncall_engineer and team.oncall_engineer in engineers:
            try:
                current_idx = engineers.index(team.oncall_engineer)
            except ValueError:
                pass
        next_idx = (current_idx + 1) % len(engineers)
        return engineers[next_idx]

    elif team.rotation_type == "custom_order":
        if not team.rotation_order:
            return None
        current_idx = team.current_rotation_index or 0
        next_idx = (current_idx + 1) % len(team.rotation_order)
        return team.rotation_order[next_idx]

    elif team.rotation_type == "weighted":
        return _calculate_weighted_next(
            team=team,
            memberships=memberships or [],
            shift_counts=shift_counts or {},
        )

    return None


def _calculate_weighted_next(
    team: "Team",
    memberships: list[dict],
    shift_counts: dict[str, int],
) -> str | None:
    """Select the next engineer using the weighted rotation strategy.

    Algorithm:
        For each eligible member, compute:
            target_ratio = member.weight / sum(all eligible weights)
            actual_ratio = shifts_completed / total_shifts  (0 on first run)
            gap = target_ratio - actual_ratio
        The member with the largest gap is selected. Ties are broken by
        whoever has gone the longest without being on-call (i.e. fewest
        shift_counts, then alphabetical slack_user_id for determinism).
    """
    eligible = [
        m for m in memberships
        if m.get("is_eligible_for_oncall", True)
    ]
    if not eligible:
        return None

    total_weight = sum(m.get("weight", 1.0) for m in eligible)
    if total_weight <= 0:
        return None

    total_shifts = sum(shift_counts.get(m["slack_user_id"], 0) for m in eligible)

    candidates: list[tuple[float, int, str]] = []
    for m in eligible:
        uid = m["slack_user_id"]
        weight = m.get("weight", 1.0)
        target_ratio = weight / total_weight
        completed = shift_counts.get(uid, 0)
        actual_ratio = completed / total_shifts if total_shifts > 0 else 0.0
        gap = target_ratio - actual_ratio
        # Sort key: largest gap first (negate for ascending sort),
        # fewest shifts first (longest since last on-call),
        # then alphabetical uid for determinism.
        candidates.append((-gap, completed, uid))

    candidates.sort()
    return candidates[0][2]


def should_rotate(team: "Team", check_date: date | None = None) -> bool:
    """Check if rotation should occur on *check_date*.

    Supports three rotation intervals via ``team.rotation_interval``:
        - ``'daily'``:    rotate every day.
        - ``'weekly'``:   rotate every 7 days from ``rotation_start_date``.
        - ``'biweekly'``: rotate every 14 days from ``rotation_start_date``.

    If ``team.handoff_day`` is set (0=Monday ... 6=Sunday), the rotation
    only fires when *check_date* falls on that day of the week.

    ``team.handoff_time`` is stored but not evaluated here (it is the
    responsibility of the scheduler / cron trigger).
    """
    if not team.rotation_enabled or not team.rotation_start_date:
        return False

    if check_date is None:
        check_date = date.today()

    # Must be on or after the start date.
    if check_date < team.rotation_start_date:
        return False

    # Handoff-day gate: if configured, only fire on that weekday.
    handoff_day = getattr(team, "handoff_day", None)
    if handoff_day is not None:
        # date.weekday(): 0=Mon, 6=Sun — matches the handoff_day convention.
        if check_date.weekday() != handoff_day:
            return False

    interval: str = getattr(team, "rotation_interval", "weekly") or "weekly"
    days_diff = (check_date - team.rotation_start_date).days

    if interval == "daily":
        # Every day is a rotation day; the period count is just days_diff.
        periods_since_start = days_diff
    elif interval == "biweekly":
        periods_since_start = days_diff // 14
    else:
        # Default: weekly (preserves original behaviour).
        periods_since_start = days_diff // 7

    # First rotation ever.
    if team.current_rotation_index is None:
        return True

    # Determine pool size for modular index comparison.
    pool_size = len(team.rotation_order) if team.rotation_order else 1
    expected_index = periods_since_start % pool_size
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


def generate_schedule_lookahead(
    team: "Team",
    rotation_engineers: list[str],
    weeks: int,
    memberships: list[dict] | None = None,
    shift_counts: dict[str, int] | None = None,
) -> list[dict]:
    """Simulate rotation for *weeks* weeks and return a projected schedule.

    This is a pure look-ahead: it does **not** persist anything or modify
    the team object.

    Args:
        team: Team with rotation configuration.
        rotation_engineers: Ordered list of Slack user IDs (for round_robin).
        weeks: Number of weeks to project.
        memberships: TeamMembership-like dicts (for weighted strategy).
        shift_counts: Current shift counts per engineer (for weighted).

    Returns:
        A list of schedule dicts, one per period::

            [{
                "week_number": 1,
                "start_date": date(2026, 2, 23),
                "end_date": date(2026, 3, 1),
                "engineer_slack_id": "U12345",
            }, ...]
    """
    if not team.rotation_enabled or not team.rotation_start_date:
        return []

    interval: str = getattr(team, "rotation_interval", "weekly") or "weekly"
    if interval == "daily":
        period_days = 1
    elif interval == "biweekly":
        period_days = 14
    else:
        period_days = 7

    # Build a mutable copy of shift_counts so weighted simulation can
    # accumulate shifts across projected periods.
    sim_shift_counts: dict[str, int] = dict(shift_counts or {})

    # Snapshot mutable team state so we can simulate without side-effects.
    sim_oncall = team.oncall_engineer
    sim_index = team.current_rotation_index

    # We need a lightweight object to pass into calculate_next_engineer
    # without mutating the real team.
    class _SimTeam:
        """Minimal stand-in for Team during simulation."""

        def __init__(self) -> None:
            self.rotation_enabled = team.rotation_enabled
            self.rotation_type = team.rotation_type
            self.rotation_order = team.rotation_order
            self.oncall_engineer = sim_oncall
            self.current_rotation_index = sim_index
            self.slack_group_id = team.slack_group_id

    sim_team = _SimTeam()

    schedule: list[dict] = []
    # Start the projection from the next period boundary after today, or
    # from rotation_start_date if it is in the future.
    today = date.today()
    if today < team.rotation_start_date:
        cursor = team.rotation_start_date
    else:
        # Align cursor to the next period boundary.
        elapsed = (today - team.rotation_start_date).days
        full_periods = elapsed // period_days
        cursor = team.rotation_start_date + timedelta(days=(full_periods + 1) * period_days)

    for week_num in range(1, weeks + 1):
        start = cursor
        end = cursor + timedelta(days=period_days - 1)

        engineer = calculate_next_engineer(
            sim_team,  # type: ignore[arg-type]
            rotation_engineers,
            memberships=memberships,
            shift_counts=sim_shift_counts,
        )

        schedule.append({
            "week_number": week_num,
            "start_date": start,
            "end_date": end,
            "engineer_slack_id": engineer,
        })

        # Advance simulation state for the next iteration.
        if engineer is not None:
            sim_team.oncall_engineer = engineer
            if team.rotation_type == "round_robin" and engineer in rotation_engineers:
                sim_team.current_rotation_index = rotation_engineers.index(engineer)
            elif team.rotation_type == "custom_order" and team.rotation_order and engineer in team.rotation_order:
                sim_team.current_rotation_index = team.rotation_order.index(engineer)
            else:
                sim_team.current_rotation_index = (sim_team.current_rotation_index or 0) + 1

            # Update simulated shift counts for weighted strategy.
            sim_shift_counts[engineer] = sim_shift_counts.get(engineer, 0) + 1

        cursor += timedelta(days=period_days)

    return schedule
