"""Live database queries for RAG context that must be real-time (not indexed).

On-call schedules, overrides, and rotations change frequently so we query them
live rather than relying on stale vector-store embeddings.
"""

import asyncio
import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from bug_bot.models.models import ServiceTeamMapping, Team

logger = logging.getLogger(__name__)


async def _resolve_slack_names(user_ids: set[str]) -> dict[str, str]:
    """Batch-resolve Slack user IDs to display names.

    Returns a mapping of user_id -> display name. Falls back to the raw ID
    if the lookup fails or Slack is not configured.
    """
    if not user_ids:
        return {}

    from bug_bot.oncall.slack_notifications import get_user_info

    results: dict[str, str] = {}
    infos = await asyncio.gather(
        *(get_user_info(uid) for uid in user_ids),
        return_exceptions=True,
    )
    for uid, info in zip(user_ids, infos):
        if isinstance(info, dict) and info:
            results[uid] = info.get("display_name") or info.get("real_name") or uid
        else:
            results[uid] = uid
    return results


async def _resolve_slack_group_names(group_ids: set[str]) -> dict[str, str]:
    """Batch-resolve Slack user-group IDs to group names.

    Fetches all workspace groups in a single API call and returns a mapping
    of group_id -> group name (or handle). Falls back to the raw ID.
    """
    if not group_ids:
        return {}

    from bug_bot.slack.user_groups import list_user_groups

    results: dict[str, str] = {gid: gid for gid in group_ids}
    try:
        groups = await list_user_groups()
        for g in groups:
            gid = g.get("id")
            if gid and gid in group_ids:
                results[gid] = g.get("name") or g.get("handle") or gid
    except Exception:
        logger.debug("Failed to resolve Slack group names", exc_info=True)
    return results


def _display(slack_id: str | None, names: dict[str, str]) -> str:
    """Return 'Display Name (UXXXXX)' or just the raw ID as fallback."""
    if not slack_id:
        return "(unknown)"
    name = names.get(slack_id)
    if name and name != slack_id:
        return f"{name} ({slack_id})"
    return slack_id


async def fetch_oncall_context(session: AsyncSession) -> str:
    """Build a text block with current on-call info for every team."""
    today = date.today()
    teams_q = await session.execute(
        select(Team).options(
            selectinload(Team.services),
            selectinload(Team.schedules),
            selectinload(Team.overrides),
        )
    )
    teams = list(teams_q.scalars().all())
    if not teams:
        return ""

    all_user_ids: set[str] = set()
    all_group_ids: set[str] = set()
    for team in teams:
        all_group_ids.add(team.slack_group_id)
        if team.oncall_engineer:
            all_user_ids.add(team.oncall_engineer)
        for svc in team.services:
            if svc.service_owner:
                all_user_ids.add(svc.service_owner)
            if svc.primary_oncall:
                all_user_ids.add(svc.primary_oncall)
            if svc.team_slack_group:
                all_group_ids.add(svc.team_slack_group)
        for o in team.overrides:
            all_user_ids.add(o.substitute_engineer_slack_id)
        for s in team.schedules:
            all_user_ids.add(s.engineer_slack_id)

    names, group_names = await asyncio.gather(
        _resolve_slack_names(all_user_ids),
        _resolve_slack_group_names(all_group_ids),
    )

    blocks: list[str] = ["=== CURRENT ON-CALL INFORMATION ==="]

    for team in teams:
        oncall_engineer = None
        source = "none"

        active_overrides = [
            o for o in team.overrides
            if o.override_date <= today and (o.end_date is None or o.end_date >= today)
        ]
        if active_overrides:
            oncall_engineer = active_overrides[0].substitute_engineer_slack_id
            source = "override"

        if not oncall_engineer:
            active_schedules = [
                s for s in team.schedules
                if s.start_date <= today and s.end_date >= today
            ]
            if active_schedules:
                oncall_engineer = active_schedules[0].engineer_slack_id
                source = "schedule"

        if not oncall_engineer and team.oncall_engineer:
            oncall_engineer = team.oncall_engineer
            source = "team_default"

        service_lines = []
        for svc in team.services:
            parts = [f"  - {svc.service_name} (repo: {svc.github_repo}, stack: {svc.tech_stack})"]
            if svc.service_owner:
                parts.append(f"    Owner: {_display(svc.service_owner, names)}")
            if svc.primary_oncall:
                parts.append(f"    Primary On-Call: {_display(svc.primary_oncall, names)}")
            service_lines.append("\n".join(parts))

        block = f"Team: {_display(team.slack_group_id, group_names)}"
        if oncall_engineer:
            block += f"\n  Current On-Call Engineer: {_display(oncall_engineer, names)} (source: {source})"
        else:
            block += "\n  Current On-Call Engineer: (none assigned)"
        if service_lines:
            block += "\n  Services:\n" + "\n".join(service_lines)
        blocks.append(block)

    return "\n\n".join(blocks)


async def fetch_service_mappings_context(session: AsyncSession) -> str:
    """Build a text block with all service-team mappings."""
    stmt = (
        select(ServiceTeamMapping)
        .options(selectinload(ServiceTeamMapping.team))
        .order_by(ServiceTeamMapping.service_name)
    )
    result = await session.execute(stmt)
    mappings = list(result.scalars().all())
    if not mappings:
        return ""

    all_user_ids: set[str] = set()
    all_group_ids: set[str] = set()
    for m in mappings:
        if m.service_owner:
            all_user_ids.add(m.service_owner)
        if m.primary_oncall:
            all_user_ids.add(m.primary_oncall)
        if m.team and m.team.oncall_engineer:
            all_user_ids.add(m.team.oncall_engineer)
        if m.team_slack_group:
            all_group_ids.add(m.team_slack_group)

    names, group_names = await asyncio.gather(
        _resolve_slack_names(all_user_ids),
        _resolve_slack_group_names(all_group_ids),
    )

    blocks: list[str] = ["=== SERVICE MAPPINGS ==="]
    for m in mappings:
        parts = [
            f"Service: {m.service_name}",
            f"  Repo: {m.github_repo}",
            f"  Tech Stack: {m.tech_stack}",
        ]
        if m.description:
            parts.append(f"  Description: {m.description}")
        if m.service_owner:
            parts.append(f"  Owner: {_display(m.service_owner, names)}")
        if m.primary_oncall:
            parts.append(f"  Primary On-Call: {_display(m.primary_oncall, names)}")
        if m.team_slack_group:
            parts.append(f"  Team: {_display(m.team_slack_group, group_names)}")
        if m.team and m.team.oncall_engineer:
            parts.append(f"  Team On-Call: {_display(m.team.oncall_engineer, names)}")
        blocks.append("\n".join(parts))

    return "\n\n".join(blocks)
