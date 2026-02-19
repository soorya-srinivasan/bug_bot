from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from bug_bot.db.repository import BugRepository
from bug_bot.db.session import async_session
from bug_bot.schemas.admin import (
    BugFilters,
    BugListItem,
    BugUpdate,
    EscalationCreate,
    EscalationResponse,
    InvestigationResponse,
    PaginatedBugs,
    PaginatedServiceTeamMappings,
    SLAConfigCreate,
    SLAConfigListResponse,
    SLAConfigResponse,
    SLAConfigUpdate,
    ServiceTeamMappingCreate,
    ServiceTeamMappingResponse,
    ServiceTeamMappingUpdate,
)


router = APIRouter()


async def get_repo() -> BugRepository:
    async with async_session() as session:
        yield BugRepository(session)


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
    category: str | None = Query(default=None),
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
        category=category,
        service=service,
        from_date=from_date,
        to_date=to_date,
        page=page,
        page_size=page_size,
        sort=sort,
    )

    items: list[BugListItem] = []
    for bug, investigation in rows:
        investigation_summary = None
        if investigation is not None:
            investigation_summary = {
                "summary": investigation.summary,
                "fix_type": investigation.fix_type,
                "confidence": investigation.confidence,
            }
        items.append(
            BugListItem(
                id=str(bug.id),
                bug_id=bug.bug_id,
                slack_channel_id=bug.slack_channel_id,
                reporter_user_id=bug.reporter_user_id,
                original_message=bug.original_message,
                severity=bug.severity,
                status=bug.status,
                category=bug.category,
                created_at=bug.created_at,
                updated_at=bug.updated_at,
                resolved_at=bug.resolved_at,
                investigation_summary=investigation_summary,
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
    if investigation is not None:
        investigation_summary = {
            "summary": investigation.summary,
            "fix_type": investigation.fix_type,
            "confidence": investigation.confidence,
        }
    return BugListItem(
        id=str(bug.id),
        bug_id=bug.bug_id,
        slack_channel_id=bug.slack_channel_id,
        reporter_user_id=bug.reporter_user_id,
        original_message=bug.original_message,
        severity=bug.severity,
        status=bug.status,
        category=bug.category,
        created_at=bug.created_at,
        updated_at=bug.updated_at,
        resolved_at=bug.resolved_at,
        investigation_summary=investigation_summary,
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
        category=payload.category,
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")

    investigation = await repo.get_investigation(bug_id)
    investigation_summary = None
    if investigation is not None:
        investigation_summary = {
            "summary": investigation.summary,
            "fix_type": investigation.fix_type,
            "confidence": investigation.confidence,
        }
    return BugListItem(
        id=str(updated.id),
        bug_id=updated.bug_id,
        slack_channel_id=updated.slack_channel_id,
        reporter_user_id=updated.reporter_user_id,
        original_message=updated.original_message,
        severity=updated.severity,
        status=updated.status,
        category=updated.category,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
        resolved_at=updated.resolved_at,
        investigation_summary=investigation_summary,
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


@router.get(
    "/service-team-mappings/{id}",
    response_model=ServiceTeamMappingResponse,
)
async def get_service_team_mapping(id: str, repo: BugRepository = Depends(get_repo)):
    m = await repo.get_service_mapping_by_id(id)
    if m is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mapping not found")
    return ServiceTeamMappingResponse(
        id=str(m.id),
        service_name=m.service_name,
        github_repo=m.github_repo,
        team_slack_group=m.team_slack_group,
        primary_oncall=m.primary_oncall,
        tech_stack=m.tech_stack,
        created_at=m.created_at,
    )


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
    return ServiceTeamMappingResponse(
        id=str(m.id),
        service_name=m.service_name,
        github_repo=m.github_repo,
        team_slack_group=m.team_slack_group,
        primary_oncall=m.primary_oncall,
        tech_stack=m.tech_stack,
        created_at=m.created_at,
    )


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
    return ServiceTeamMappingResponse(
        id=str(m.id),
        service_name=m.service_name,
        github_repo=m.github_repo,
        team_slack_group=m.team_slack_group,
        primary_oncall=m.primary_oncall,
        tech_stack=m.tech_stack,
        created_at=m.created_at,
    )


@router.delete("/service-team-mappings/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service_team_mapping(id: str, repo: BugRepository = Depends(get_repo)):
    await repo.delete_service_mapping(id)
    return None

