import asyncio
from datetime import datetime, date

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status

from bug_bot.db.repository import BugRepository
from bug_bot.db.session import async_session
from bug_bot.oncall import service as oncall_service
from bug_bot.schemas.admin import (
    BugConversationListResponse,
    BugConversationResponse,
    BugFilters,
    BugListItem,
    BugUpdate,
    CurrentOnCallResponse,
    EscalationCreate,
    EscalationResponse,
    InvestigationFindingListResponse,
    InvestigationFindingResponse,
    InvestigationResponse,
    OnCallHistoryResponse,
    OnCallScheduleCreate,
    OnCallScheduleResponse,
    OnCallScheduleUpdate,
    PaginatedBugs,
    PaginatedOnCallHistory,
    PaginatedOnCallSchedules,
    PaginatedTeams,
    PaginatedServiceTeamMappings,
    SLAConfigCreate,
    SLAConfigListResponse,
    SLAConfigResponse,
    SLAConfigUpdate,
    TaggedOnEntry,
    TeamCreate,
    TeamResponse,
    TeamRotationConfigUpdate,
    TeamSummary,
    TeamUpdate,
    ServiceTeamMappingCreate,
    ServiceTeamMappingResponse,
    ServiceTeamMappingUpdate,
    SlackUserDetail,
    SlackUserGroupListItem,
    SlackUserGroupListResponse,
    SlackUserGroupUsersRequest,
    SlackUserGroupUsersResponse,
    SlackUsersLookupResponse,
)
from bug_bot.oncall.slack_notifications import get_user_info
from bug_bot.slack.user_groups import list_user_groups, list_users_in_group


router = APIRouter()


def _slack_message_url(channel_id: str, thread_ts: str) -> str:
    """Build Slack deep link to the bug report message (opens in user's workspace)."""
    ts_no_dot = thread_ts.replace(".", "")
    return f"https://slack.com/archives/{channel_id}/p{ts_no_dot}"


async def get_repo() -> BugRepository:
    async with async_session() as session:
        yield BugRepository(session)


async def _resolve_tagged_on(
    repo: BugRepository, relevant_services: list[str] | None,
) -> list[TaggedOnEntry]:
    if not relevant_services:
        return []
    mappings = await repo.get_service_mappings_by_names(relevant_services)
    seen: set[str] = set()
    entries: list[TaggedOnEntry] = []
    for m in mappings:
        oncall = None
        if m.team and m.team.oncall_engineer:
            oncall = m.team.oncall_engineer
        if not oncall:
            oncall = m.primary_oncall
        service_owner = m.service_owner
        slack_group_id = m.team.slack_group_id if m.team else None
        key = oncall or service_owner or slack_group_id or ""
        if key and key not in seen:
            seen.add(key)
            entries.append(TaggedOnEntry(
                oncall_engineer=oncall,
                service_owner=service_owner,
                slack_group_id=slack_group_id,
            ))
    return entries


def _validate_status_transition(current: str, new: str) -> None:
    if current == new:
        return
    allowed = {
        "new": {"triaged"},
        "triaged": {"investigating"},
        "investigating": {"awaiting_dev", "resolved"},
        "awaiting_dev": {"escalated", "resolved"},
        "escalated": {"resolved"},
        "resolved": set(),
    }
    if current not in allowed or new not in allowed[current]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status transition {current!r} -> {new!r}",
        )


@router.get("/bugs", response_model=PaginatedBugs)
async def list_bugs(
    *,
    repo: BugRepository = Depends(get_repo),
    status: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    service: str | None = Query(default=None),
    from_date: datetime | None = Query(default=None),
    to_date: datetime | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort: str = Query("-created_at"),
):
    rows, total = await repo.list_bugs(
        status=status,
        severity=severity,
        service=service,
        from_date=from_date,
        to_date=to_date,
        page=page,
        page_size=page_size,
        sort=sort,
    )

    all_services: set[str] = set()
    for _bug, inv in rows:
        if inv and inv.relevant_services:
            all_services.update(inv.relevant_services)

    service_mappings = await repo.get_service_mappings_by_names(list(all_services)) if all_services else []
    svc_map: dict[str, list] = {}
    for m in service_mappings:
        svc_map.setdefault(m.service_name.lower(), []).append(m)

    items: list[BugListItem] = []
    for bug, investigation in rows:
        investigation_summary = None
        tagged_on: list[TaggedOnEntry] = []
        if investigation is not None:
            investigation_summary = {
                "summary": investigation.summary,
                "fix_type": investigation.fix_type,
                "confidence": investigation.confidence,
            }
            seen: set[str] = set()
            for svc in (investigation.relevant_services or []):
                for m in svc_map.get(svc.lower(), []):
                    oncall = None
                    if m.team and m.team.oncall_engineer:
                        oncall = m.team.oncall_engineer
                    if not oncall:
                        oncall = m.primary_oncall
                    service_owner = m.service_owner
                    slack_group_id = m.team.slack_group_id if m.team else None
                    key = oncall or service_owner or slack_group_id or ""
                    if key and key not in seen:
                        seen.add(key)
                        tagged_on.append(TaggedOnEntry(
                            oncall_engineer=oncall,
                            service_owner=service_owner,
                            slack_group_id=slack_group_id,
                        ))

        items.append(
            BugListItem(
                id=str(bug.id),
                bug_id=bug.bug_id,
                slack_channel_id=bug.slack_channel_id,
                slack_thread_ts=bug.slack_thread_ts,
                slack_message_url=_slack_message_url(bug.slack_channel_id, bug.slack_thread_ts),
                reporter_user_id=bug.reporter_user_id,
                original_message=bug.original_message,
                severity=bug.severity,
                status=bug.status,
                created_at=bug.created_at,
                updated_at=bug.updated_at,
                resolved_at=bug.resolved_at,
                investigation_summary=investigation_summary,
                tagged_on=tagged_on,
            )
        )

    return PaginatedBugs(items=items, total=total, page=page, page_size=page_size)


@router.get("/bugs/{bug_id}", response_model=BugListItem)
async def get_bug_detail(bug_id: str, repo: BugRepository = Depends(get_repo)):
    bug = await repo.get_bug_by_id(bug_id)
    if bug is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")
    investigation = await repo.get_investigation(bug_id)
    investigation_summary = None
    tagged_on: list[TaggedOnEntry] = []
    if investigation is not None:
        investigation_summary = {
            "summary": investigation.summary,
            "fix_type": investigation.fix_type,
            "confidence": investigation.confidence,
        }
        tagged_on = await _resolve_tagged_on(repo, investigation.relevant_services)
    return BugListItem(
        id=str(bug.id),
        bug_id=bug.bug_id,
        slack_channel_id=bug.slack_channel_id,
        slack_thread_ts=bug.slack_thread_ts,
        slack_message_url=_slack_message_url(bug.slack_channel_id, bug.slack_thread_ts),
        reporter_user_id=bug.reporter_user_id,
        original_message=bug.original_message,
        severity=bug.severity,
        status=bug.status,
        created_at=bug.created_at,
        updated_at=bug.updated_at,
        resolved_at=bug.resolved_at,
        investigation_summary=investigation_summary,
        tagged_on=tagged_on,
    )


@router.patch("/bugs/{bug_id}", response_model=BugListItem)
async def update_bug(bug_id: str, payload: BugUpdate, repo: BugRepository = Depends(get_repo)):
    bug = await repo.get_bug_by_id(bug_id)
    if bug is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")

    new_status = payload.status or bug.status
    if payload.status is not None:
        _validate_status_transition(bug.status, payload.status)

    updated = await repo.update_bug_admin(
        bug_id,
        severity=payload.severity,
        status=new_status if payload.status is not None else None,
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")

    investigation = await repo.get_investigation(bug_id)
    investigation_summary = None
    tagged_on: list[TaggedOnEntry] = []
    if investigation is not None:
        investigation_summary = {
            "summary": investigation.summary,
            "fix_type": investigation.fix_type,
            "confidence": investigation.confidence,
        }
        tagged_on = await _resolve_tagged_on(repo, investigation.relevant_services)
    return BugListItem(
        id=str(updated.id),
        bug_id=updated.bug_id,
        slack_channel_id=updated.slack_channel_id,
        slack_thread_ts=updated.slack_thread_ts,
        slack_message_url=_slack_message_url(updated.slack_channel_id, updated.slack_thread_ts),
        reporter_user_id=updated.reporter_user_id,
        original_message=updated.original_message,
        severity=updated.severity,
        status=updated.status,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
        resolved_at=updated.resolved_at,
        investigation_summary=investigation_summary,
        tagged_on=tagged_on,
    )


@router.get("/bugs/{bug_id}/investigation", response_model=InvestigationResponse)
async def get_investigation(bug_id: str, repo: BugRepository = Depends(get_repo)):
    investigation = await repo.get_investigation(bug_id)
    if investigation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    return InvestigationResponse(
        bug_id=bug_id,
        root_cause=investigation.root_cause,
        fix_type=investigation.fix_type,
        pr_url=investigation.pr_url,
        summary=investigation.summary,
        confidence=investigation.confidence,
        relevant_services=investigation.relevant_services or [],
        recommended_actions=investigation.recommended_actions or [],
        cost_usd=investigation.cost_usd,
        duration_ms=investigation.duration_ms,
        conversation_history=investigation.conversation_history,
        summary_thread_ts=investigation.summary_thread_ts,
        created_at=investigation.created_at,
    )


@router.post(
    "/bugs/{bug_id}/escalations",
    status_code=status.HTTP_201_CREATED,
    response_model=EscalationResponse,
)
async def create_escalation(
    bug_id: str, payload: EscalationCreate, repo: BugRepository = Depends(get_repo)
):
    bug = await repo.get_bug_by_id(bug_id)
    if bug is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")

    escalation = await repo.create_escalation(
        bug_id,
        escalation_level=payload.escalation_level,
        escalated_to=payload.escalated_to,
        reason=payload.reason,
    )
    return EscalationResponse(
        id=str(escalation.id),
        bug_id=escalation.bug_id,
        escalation_level=escalation.escalation_level,
        escalated_to=escalation.escalated_to,
        reason=escalation.reason,
        created_at=escalation.created_at,
    )


@router.get("/bugs/{bug_id}/conversations", response_model=BugConversationListResponse)
async def get_bug_conversations(bug_id: str, repo: BugRepository = Depends(get_repo)):
    bug = await repo.get_bug_by_id(bug_id)
    if bug is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")
    rows = await repo.get_conversations(bug_id)
    items = [
        BugConversationResponse(
            id=str(c.id),
            bug_id=c.bug_id,
            channel=c.channel,
            sender_type=c.sender_type,
            sender_id=c.sender_id,
            message_text=c.message_text,
            message_type=c.message_type,
            metadata=c.metadata_,
            created_at=c.created_at,
        )
        for c in rows
    ]
    return BugConversationListResponse(items=items)


@router.get("/bugs/{bug_id}/findings", response_model=InvestigationFindingListResponse)
async def get_bug_findings(bug_id: str, repo: BugRepository = Depends(get_repo)):
    bug = await repo.get_bug_by_id(bug_id)
    if bug is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")
    rows = await repo.get_findings_for_bug(bug_id)
    items = [
        InvestigationFindingResponse(
            id=str(f.id),
            bug_id=f.bug_id,
            category=f.category,
            finding=f.finding,
            severity=f.severity,
            created_at=f.created_at,
        )
        for f in rows
    ]
    return InvestigationFindingListResponse(items=items)


@router.get("/sla-configs", response_model=SLAConfigListResponse)
async def list_sla_configs(
    is_active: bool | None = Query(default=None),
    repo: BugRepository = Depends(get_repo),
):
    configs = await repo.list_sla_configs(is_active=is_active)
    items = [
        SLAConfigResponse(
            id=str(c.id),
            severity=c.severity,
            acknowledgement_target_min=c.acknowledgement_target_min,
            resolution_target_min=c.resolution_target_min,
            follow_up_interval_min=c.follow_up_interval_min,
            escalation_threshold=c.escalation_threshold,
            escalation_contacts=c.escalation_contacts or [],
            is_active=c.is_active,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in configs
    ]
    return SLAConfigListResponse(items=items)


@router.get("/sla-configs/{id}", response_model=SLAConfigResponse)
async def get_sla_config(id: str, repo: BugRepository = Depends(get_repo)):
    config = await repo.get_sla_config_by_id(id)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SLA config not found")
    return SLAConfigResponse(
        id=str(config.id),
        severity=config.severity,
        acknowledgement_target_min=config.acknowledgement_target_min,
        resolution_target_min=config.resolution_target_min,
        follow_up_interval_min=config.follow_up_interval_min,
        escalation_threshold=config.escalation_threshold,
        escalation_contacts=config.escalation_contacts or [],
        is_active=config.is_active,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.post("/sla-configs", status_code=status.HTTP_201_CREATED, response_model=SLAConfigResponse)
async def create_sla_config(
    payload: SLAConfigCreate, repo: BugRepository = Depends(get_repo)
):
    data = payload.model_dump()
    config = await repo.create_sla_config(data)
    return SLAConfigResponse(
        id=str(config.id),
        severity=config.severity,
        acknowledgement_target_min=config.acknowledgement_target_min,
        resolution_target_min=config.resolution_target_min,
        follow_up_interval_min=config.follow_up_interval_min,
        escalation_threshold=config.escalation_threshold,
        escalation_contacts=config.escalation_contacts or [],
        is_active=config.is_active,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.patch(
    "/sla-configs/{id}",
    response_model=SLAConfigResponse,
)
async def update_sla_config(
    id: str, payload: SLAConfigUpdate, repo: BugRepository = Depends(get_repo)
):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    config = await repo.update_sla_config(id, data)
    if config is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="SLA config not found")
    return SLAConfigResponse(
        id=str(config.id),
        severity=config.severity,
        acknowledgement_target_min=config.acknowledgement_target_min,
        resolution_target_min=config.resolution_target_min,
        follow_up_interval_min=config.follow_up_interval_min,
        escalation_threshold=config.escalation_threshold,
        escalation_contacts=config.escalation_contacts or [],
        is_active=config.is_active,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )


@router.delete("/sla-configs/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_sla_config(id: str, repo: BugRepository = Depends(get_repo)):
    await repo.delete_sla_config(id)
    return None


@router.get(
    "/service-team-mappings",
    response_model=PaginatedServiceTeamMappings,
)
async def list_service_team_mappings(
    *,
    repo: BugRepository = Depends(get_repo),
    service_name: str | None = Query(default=None),
    tech_stack: str | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    items, total = await repo.list_service_mappings(
        service_name=service_name,
        tech_stack=tech_stack,
        page=page,
        page_size=page_size,
    )
    result_items = [
        ServiceTeamMappingResponse(
            id=str(m.id),
            service_name=m.service_name,
            github_repo=m.github_repo,
            team_slack_group=m.team_slack_group,
            primary_oncall=m.primary_oncall,
            tech_stack=m.tech_stack,
            service_owner=m.service_owner,
            team_id=str(m.team_id) if m.team_id else None,
            created_at=m.created_at,
        )
        for m in items
    ]
    return PaginatedServiceTeamMappings(
        items=result_items,
        total=total,
        page=page,
        page_size=page_size,
    )


def _mapping_response(m) -> ServiceTeamMappingResponse:
    team_summary = None
    if m.team is not None:
        team_summary = TeamSummary(
            id=str(m.team.id),
            slack_group_id=m.team.slack_group_id,
            oncall_engineer=m.team.oncall_engineer,
        )
    return ServiceTeamMappingResponse(
        id=str(m.id),
        service_name=m.service_name,
        github_repo=m.github_repo,
        team_slack_group=m.team_slack_group,
        primary_oncall=m.primary_oncall,
        tech_stack=m.tech_stack,
        service_owner=m.service_owner,
        team_id=str(m.team_id) if m.team_id else None,
        created_at=m.created_at,
        team=team_summary,
    )


@router.get(
    "/service-team-mappings/{id}",
    response_model=ServiceTeamMappingResponse,
)
async def get_service_team_mapping(id: str, repo: BugRepository = Depends(get_repo)):
    m = await repo.get_service_mapping_by_id(id)
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")
    return _mapping_response(m)


@router.post(
    "/service-team-mappings",
    status_code=status.HTTP_201_CREATED,
    response_model=ServiceTeamMappingResponse,
)
async def create_service_team_mapping(
    payload: ServiceTeamMappingCreate,
    repo: BugRepository = Depends(get_repo),
):
    data = payload.model_dump()
    m = await repo.create_service_mapping(data)
    # Reload to get the eager-loaded team relationship
    m = await repo.get_service_mapping_by_id(str(m.id))
    return _mapping_response(m)


@router.patch(
    "/service-team-mappings/{id}",
    response_model=ServiceTeamMappingResponse,
)
async def update_service_team_mapping(
    id: str,
    payload: ServiceTeamMappingUpdate,
    repo: BugRepository = Depends(get_repo),
):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    m = await repo.update_service_mapping(id, data)
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")
    # Reload to get the eager-loaded team relationship
    m = await repo.get_service_mapping_by_id(str(m.id))
    return _mapping_response(m)


@router.delete("/service-team-mappings/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service_team_mapping(id: str, repo: BugRepository = Depends(get_repo)):
    await repo.delete_service_mapping(id)
    return None


# --- Teams ---


@router.get("/teams", response_model=PaginatedTeams)
async def list_teams(
    *,
    repo: BugRepository = Depends(get_repo),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    items, total = await repo.list_teams(page=page, page_size=page_size)
    result_items = [
        TeamResponse(
            id=str(t.id),
            slack_group_id=t.slack_group_id,
            oncall_engineer=t.oncall_engineer,
            rotation_enabled=t.rotation_enabled,
            rotation_type=t.rotation_type,
            rotation_order=t.rotation_order,
            rotation_start_date=t.rotation_start_date,
            current_rotation_index=t.current_rotation_index,
            created_at=t.created_at,
            updated_at=t.updated_at,
        )
        for t in items
    ]
    return PaginatedTeams(items=result_items, total=total, page=page, page_size=page_size)


@router.post("/teams", status_code=status.HTTP_201_CREATED, response_model=TeamResponse)
async def create_team(
    payload: TeamCreate,
    repo: BugRepository = Depends(get_repo),
):
    data = payload.model_dump()
    t = await repo.create_team(data)
    return TeamResponse(
        id=str(t.id),
        slack_group_id=t.slack_group_id,
        oncall_engineer=t.oncall_engineer,
        rotation_enabled=t.rotation_enabled,
        rotation_type=t.rotation_type,
        rotation_order=t.rotation_order,
        rotation_start_date=t.rotation_start_date,
        current_rotation_index=t.current_rotation_index,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


@router.get("/teams/{id}", response_model=TeamResponse)
async def get_team(id: str, repo: BugRepository = Depends(get_repo)):
    t = await repo.get_team_by_id(id)
    if t is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    return TeamResponse(
        id=str(t.id),
        slack_group_id=t.slack_group_id,
        oncall_engineer=t.oncall_engineer,
        rotation_enabled=t.rotation_enabled,
        rotation_type=t.rotation_type,
        rotation_order=t.rotation_order,
        rotation_start_date=t.rotation_start_date,
        current_rotation_index=t.current_rotation_index,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


@router.patch("/teams/{id}", response_model=TeamResponse)
async def update_team(
    id: str,
    payload: TeamUpdate,
    repo: BugRepository = Depends(get_repo),
):
    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    t = await repo.update_team(id, data)
    if t is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    return TeamResponse(
        id=str(t.id),
        slack_group_id=t.slack_group_id,
        oncall_engineer=t.oncall_engineer,
        rotation_enabled=t.rotation_enabled,
        rotation_type=t.rotation_type,
        rotation_order=t.rotation_order,
        rotation_start_date=t.rotation_start_date,
        current_rotation_index=t.current_rotation_index,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


@router.delete("/teams/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(id: str, repo: BugRepository = Depends(get_repo)):
    await repo.delete_team(id)
    return None


# --- On-Call Scheduling ---


@router.get(
    "/teams/{team_id}/oncall-schedules",
    response_model=PaginatedOnCallSchedules,
)
async def list_oncall_schedules(
    team_id: str,
    repo: BugRepository = Depends(get_repo),
    start_date: date | None = Query(default=None, description="Filter schedules starting on or after this date"),
    end_date: date | None = Query(default=None, description="Filter schedules ending on or before this date"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """List on-call schedules for a team."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    items, total = await repo.get_oncall_schedules_by_team(
        team_id=team_id,
        start_date=start_date,
        end_date=end_date,
        page=page,
        page_size=page_size,
    )
    result_items = [
        OnCallScheduleResponse(
            id=str(s.id),
            team_id=str(s.team_id),
            engineer_slack_id=s.engineer_slack_id,
            start_date=s.start_date,
            end_date=s.end_date,
            schedule_type=s.schedule_type,
            days_of_week=s.days_of_week,
            created_by=s.created_by,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in items
    ]
    return PaginatedOnCallSchedules(
        items=result_items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/teams/{team_id}/oncall-schedules",
    status_code=status.HTTP_201_CREATED,
    response_model=OnCallScheduleResponse,
)
async def create_oncall_schedule(
    team_id: str,
    payload: OnCallScheduleCreate,
    created_by: str = Query(..., description="Slack user ID of the person creating the schedule"),
    repo: BugRepository = Depends(get_repo),
):
    """Create a new on-call schedule."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    try:
        schedule = await oncall_service.assign_oncall(
            repo=repo,
            team_id=team_id,
            engineer_slack_id=payload.engineer_slack_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            schedule_type=payload.schedule_type,
            created_by=created_by,
            days_of_week=payload.days_of_week,
            send_notification=True,
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return OnCallScheduleResponse(
        id=str(schedule.id),
        team_id=str(schedule.team_id),
        engineer_slack_id=schedule.engineer_slack_id,
        start_date=schedule.start_date,
        end_date=schedule.end_date,
        schedule_type=schedule.schedule_type,
        days_of_week=schedule.days_of_week,
        created_by=schedule.created_by,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


@router.get(
    "/teams/{team_id}/oncall-schedules/{schedule_id}",
    response_model=OnCallScheduleResponse,
)
async def get_oncall_schedule(
    team_id: str,
    schedule_id: str,
    repo: BugRepository = Depends(get_repo),
):
    """Get a single on-call schedule."""
    schedule = await repo.get_oncall_schedule_by_id(schedule_id)
    if schedule is None or str(schedule.team_id) != team_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")
    return OnCallScheduleResponse(
        id=str(schedule.id),
        team_id=str(schedule.team_id),
        engineer_slack_id=schedule.engineer_slack_id,
        start_date=schedule.start_date,
        end_date=schedule.end_date,
        schedule_type=schedule.schedule_type,
        days_of_week=schedule.days_of_week,
        created_by=schedule.created_by,
        created_at=schedule.created_at,
        updated_at=schedule.updated_at,
    )


@router.patch(
    "/teams/{team_id}/oncall-schedules/{schedule_id}",
    response_model=OnCallScheduleResponse,
)
async def update_oncall_schedule(
    team_id: str,
    schedule_id: str,
    payload: OnCallScheduleUpdate,
    repo: BugRepository = Depends(get_repo),
):
    """Update an on-call schedule."""
    schedule = await repo.get_oncall_schedule_by_id(schedule_id)
    if schedule is None or str(schedule.team_id) != team_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    # Check for overlaps if dates are being updated
    update_data = {k: v for k, v in payload.model_dump().items() if v is not None}
    start_date = update_data.get("start_date", schedule.start_date)
    end_date = update_data.get("end_date", schedule.end_date)

    if await repo.check_schedule_overlap(team_id, start_date, end_date, exclude_id=schedule_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Updated schedule overlaps with existing schedule",
        )

    updated = await repo.update_oncall_schedule(schedule_id, update_data)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    # Log history
    await repo.log_oncall_change(
        team_id=team_id,
        engineer_slack_id=updated.engineer_slack_id,
        change_type="schedule_updated",
        effective_date=updated.start_date,
        previous_engineer_slack_id=schedule.engineer_slack_id if schedule.engineer_slack_id != updated.engineer_slack_id else None,
        change_reason="Schedule updated",
    )

    return OnCallScheduleResponse(
        id=str(updated.id),
        team_id=str(updated.team_id),
        engineer_slack_id=updated.engineer_slack_id,
        start_date=updated.start_date,
        end_date=updated.end_date,
        schedule_type=updated.schedule_type,
        days_of_week=updated.days_of_week,
        created_by=updated.created_by,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
    )


@router.delete(
    "/teams/{team_id}/oncall-schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_oncall_schedule(
    team_id: str,
    schedule_id: str,
    repo: BugRepository = Depends(get_repo),
):
    """Delete an on-call schedule."""
    schedule = await repo.get_oncall_schedule_by_id(schedule_id)
    if schedule is None or str(schedule.team_id) != team_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule not found")

    # Log history before deleting
    await repo.log_oncall_change(
        team_id=team_id,
        engineer_slack_id=schedule.engineer_slack_id,
        change_type="schedule_deleted",
        effective_date=schedule.start_date,
        previous_engineer_slack_id=schedule.engineer_slack_id,
        change_reason="Schedule deleted",
    )

    await repo.delete_oncall_schedule(schedule_id)
    return None


@router.get(
    "/teams/{team_id}/current-oncall",
    response_model=CurrentOnCallResponse,
)
async def get_current_oncall(
    team_id: str,
    check_date: date | None = Query(default=None, description="Date to check (defaults to today)"),
    repo: BugRepository = Depends(get_repo),
):
    """Get current on-call engineer for a team."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    current = await oncall_service.get_current_oncall(repo, team_id, check_date)
    if current is None:
        return CurrentOnCallResponse(
            engineer_slack_id=None,
            effective_date=None,
            source=None,
            schedule_id=None,
        )

    return CurrentOnCallResponse(
        engineer_slack_id=current.get("engineer_slack_id"),
        effective_date=current.get("effective_date"),
        source=current.get("source"),
        schedule_id=current.get("schedule_id"),
    )


@router.get(
    "/teams/{team_id}/oncall-history",
    response_model=PaginatedOnCallHistory,
)
async def get_oncall_history(
    team_id: str,
    repo: BugRepository = Depends(get_repo),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """Get on-call history for a team."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    items, total = await repo.get_oncall_history(
        team_id=team_id,
        page=page,
        page_size=page_size,
    )
    result_items = [
        OnCallHistoryResponse(
            id=str(h.id),
            team_id=str(h.team_id),
            engineer_slack_id=h.engineer_slack_id,
            previous_engineer_slack_id=h.previous_engineer_slack_id,
            change_type=h.change_type,
            change_reason=h.change_reason,
            effective_date=h.effective_date,
            changed_by=h.changed_by,
            created_at=h.created_at,
        )
        for h in items
    ]
    return PaginatedOnCallHistory(
        items=result_items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.patch(
    "/teams/{team_id}/rotation-config",
    response_model=TeamResponse,
)
async def update_rotation_config(
    team_id: str,
    payload: TeamRotationConfigUpdate,
    repo: BugRepository = Depends(get_repo),
):
    """Update rotation configuration for a team."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    updated = await repo.update_team(team_id, data)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    return TeamResponse(
        id=str(updated.id),
        slack_group_id=updated.slack_group_id,
        oncall_engineer=updated.oncall_engineer,
        rotation_enabled=updated.rotation_enabled,
        rotation_type=updated.rotation_type,
        rotation_order=updated.rotation_order,
        rotation_start_date=updated.rotation_start_date,
        current_rotation_index=updated.current_rotation_index,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
    )


@router.post(
    "/teams/{team_id}/rotate",
    response_model=TeamResponse,
)
async def trigger_rotation(
    team_id: str,
    repo: BugRepository = Depends(get_repo),
):
    """Manually trigger rotation for a team."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    if not team.rotation_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rotation is not enabled for this team",
        )

    rotated = await oncall_service.process_auto_rotation(repo, team_id)
    if not rotated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Rotation could not be applied (check rotation configuration)",
        )

    # Reload team to get updated values
    updated = await repo.get_team_by_id(team_id)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    return TeamResponse(
        id=str(updated.id),
        slack_group_id=updated.slack_group_id,
        oncall_engineer=updated.oncall_engineer,
        rotation_enabled=updated.rotation_enabled,
        rotation_type=updated.rotation_type,
        rotation_order=updated.rotation_order,
        rotation_start_date=updated.rotation_start_date,
        current_rotation_index=updated.current_rotation_index,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
    )


# --- Slack user groups (admin) ---


@router.get("/slack/user-groups", response_model=SlackUserGroupListResponse)
async def get_slack_user_groups(
    include_disabled: bool = Query(False, description="Include disabled user groups"),
):
    """List Slack user groups (mention groups) in the workspace."""
    try:
        raw = await list_user_groups(include_disabled=include_disabled)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Slack API error: {e!s}",
        ) from e
    items = [
        SlackUserGroupListItem(
            id=g["id"],
            name=g.get("name", ""),
            handle=g.get("handle", ""),
            description=g.get("description") or None,
            user_count=g.get("user_count"),
            date_create=g.get("date_create"),
        )
        for g in raw
    ]
    return SlackUserGroupListResponse(items=items)


@router.post(
    "/slack/user-groups/users",
    response_model=SlackUserGroupUsersResponse,
)
async def get_slack_user_group_users(
    payload: SlackUserGroupUsersRequest = Body(
        ...,
        example={"usergroup_id": "S01234567"},
        description="Request body containing the Slack user group ID.",
    ),
    include_disabled: bool = Query(False, description="Include disabled user groups"),
    include_user_details: bool = Query(
        True,
        description="Fetch real_name, display_name, etc. for each user",
    ),
):
    """List users in a Slack user group. Group ID is provided in the request body."""
    try:
        raw = await list_users_in_group(
            payload.usergroup_id,
            include_disabled=include_disabled,
            include_user_details=include_user_details,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Slack API error: {e!s}",
        ) from e
    users = None
    if raw.get("users") is not None:
        users = [
            SlackUserDetail(
                id=u["id"],
                name=u.get("name"),
                real_name=u.get("real_name"),
                display_name=u.get("display_name"),
                is_bot=u.get("is_bot", False),
                deleted=u.get("deleted", False),
            )
            for u in raw["users"]
        ]
    return SlackUserGroupUsersResponse(
        usergroup_id=raw["usergroup_id"],
        user_ids=raw["user_ids"],
        users=users,
    )


@router.get("/slack/users/lookup", response_model=SlackUsersLookupResponse)
async def lookup_slack_users(
    ids: str = Query(..., description="Comma-separated Slack user IDs"),
):
    """Batch-resolve Slack user IDs to display names."""
    from bug_bot.oncall.slack_notifications import _get_slack_client, _slack_configured

    user_ids = [uid.strip() for uid in ids.split(",") if uid.strip()]
    if not user_ids:
        return SlackUsersLookupResponse(users={}, team_id=None)

    if not _slack_configured():
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Slack is not configured",
        )

    unique_ids = list(dict.fromkeys(user_ids))

    results: dict[str, SlackUserDetail] = {}
    infos = await asyncio.gather(
        *(get_user_info(uid) for uid in unique_ids),
        return_exceptions=True,
    )
    for uid, info in zip(unique_ids, infos):
        if isinstance(info, dict) and info:
            results[uid] = SlackUserDetail(
                id=info["id"],
                name=info.get("name"),
                real_name=info.get("real_name"),
                display_name=info.get("display_name"),
                is_bot=info.get("is_bot", False),
                deleted=info.get("deleted", False),
            )

    team_id: str | None = None
    try:
        client = _get_slack_client()
        auth = await client.auth_test()
        if auth.get("ok"):
            team_id = auth.get("team_id")
    except Exception:
        pass

    return SlackUsersLookupResponse(users=results, team_id=team_id)

