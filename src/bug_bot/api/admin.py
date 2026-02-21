import asyncio
import logging
from datetime import datetime, date, time

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.exc import ProgrammingError

from bug_bot.db.repository import BugRepository
from bug_bot.db.session import async_session
from bug_bot.oncall import service as oncall_service

logger = logging.getLogger(__name__)
from bug_bot.schemas.admin import (
    AuditLogListResponse,
    AuditLogResponse,
    BugConversationListResponse,
    BugConversationResponse,
    BugFilters,
    BugListItem,
    BugUpdate,
    CurrentOnCallResponse,
    DashboardResponse,
    EscalationCreate,
    EscalationResponse,
    GlobalOnCallResponse,
    InvestigationFindingListResponse,
    InvestigationFindingResponse,
    InvestigationFollowupListResponse,
    InvestigationFollowupResponse,
    InvestigationMessageResponse,
    InvestigationResponse,
    NudgeResponse,
    OnCallAuditLogResponse,
    OnCallHistoryResponse,
    OnCallOverrideCreate,
    OnCallOverrideResponse,
    OnCallScheduleCreate,
    OnCallScheduleResponse,
    OnCallScheduleUpdate,
    OverrideStatusUpdate,
    PaginatedBugs,
    PaginatedOnCallAuditLogs,
    PaginatedOnCallHistory,
    PaginatedOnCallOverrides,
    PaginatedOnCallSchedules,
    PaginatedTeams,
    PaginatedServiceTeamMappings,
    RotationGenerateRequest,
    RotationPreviewEntry,
    RotationPreviewResponse,
    SLAConfigCreate,
    SLAConfigListResponse,
    SLAConfigResponse,
    SLAConfigUpdate,
    TaggedOnEntry,
    TeamCreate,
    TeamMembershipResponse,
    TeamMembershipUpdate,
    TeamMembershipUpsert,
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
from bug_bot.oncall.slack_notifications import get_user_info, send_nudge
from bug_bot.slack.user_groups import list_user_groups, list_users_in_group


router = APIRouter()


def _slack_message_url(channel_id: str, thread_ts: str) -> str:
    """Build Slack deep link to the bug report message (opens in user's workspace)."""
    ts_no_dot = thread_ts.replace(".", "")
    return f"https://slack.com/archives/{channel_id}/p{ts_no_dot}"


async def get_repo() -> BugRepository:
    async with async_session() as session:
        yield BugRepository(session)


@router.get("/dashboard", response_model=DashboardResponse)
async def get_dashboard(repo: BugRepository = Depends(get_repo)):
    """Aggregated analytics for the dashboard page."""
    stats = await repo.get_dashboard_stats()
    return DashboardResponse(**stats)


async def _resolve_tagged_on(
    repo: BugRepository,
    relevant_services: list[str] | None,
    as_of_date: date | None = None,
) -> list[TaggedOnEntry]:
    """Resolve on-call entries for the given services.

    When as_of_date is set, uses on-call as of that date (for historical tagged_on).
    When as_of_date is None, uses current/today (e.g. for nudge).
    """
    if not relevant_services:
        return []
    check_date = as_of_date if as_of_date is not None else date.today()
    raw = await repo.get_oncall_for_services(relevant_services, check_date=check_date)
    return [
        TaggedOnEntry(
            oncall_engineer=e.get("oncall_engineer"),
            service_owner=e.get("service_owner"),
            slack_group_id=e.get("slack_group_id"),
        )
        for e in raw
    ]


def _validate_status_transition(current: str, new: str) -> None:
    if current == new:
        return
    allowed = {
        "new": {"triaged"},
        "triaged": {"investigating"},
        "investigating": {"awaiting_dev", "resolved"},
        "awaiting_dev": {"escalated", "resolved"},
        "escalated": {"resolved"},
        "dev_takeover": {"resolved"},
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
    bug_id: str | None = Query(default=None),
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
        bug_id=bug_id,
        status=status,
        severity=severity,
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
        tagged_on: list[TaggedOnEntry] = []
        if investigation is not None:
            investigation_summary = {
                "summary": investigation.summary,
                "fix_type": investigation.fix_type,
                "confidence": investigation.confidence,
            }
            as_of = bug.created_at.date() if bug.created_at else date.today()
            tagged_on = await _resolve_tagged_on(
                repo, investigation.relevant_services or [], as_of_date=as_of
            )
            current_on_call = await _resolve_tagged_on(
                repo, investigation.relevant_services or [], as_of_date=None
            )
        else:
            current_on_call = []

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
                assignee_user_id=bug.assignee_user_id,
                resolution_type=bug.resolution_type,
                closure_reason=bug.closure_reason,
                fix_provided=bug.fix_provided,
                investigation_summary=investigation_summary,
                tagged_on=tagged_on,
                current_on_call=current_on_call,
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
    current_on_call: list[TaggedOnEntry] = []
    if investigation is not None:
        investigation_summary = {
            "summary": investigation.summary,
            "fix_type": investigation.fix_type,
            "confidence": investigation.confidence,
        }
        as_of = bug.created_at.date() if bug.created_at else date.today()
        tagged_on = await _resolve_tagged_on(
            repo, investigation.relevant_services or [], as_of_date=as_of
        )
        current_on_call = await _resolve_tagged_on(
            repo, investigation.relevant_services or [], as_of_date=None
        )
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
        assignee_user_id=bug.assignee_user_id,
        resolution_type=bug.resolution_type,
        closure_reason=bug.closure_reason,
        fix_provided=bug.fix_provided,
        investigation_summary=investigation_summary,
        tagged_on=tagged_on,
        current_on_call=current_on_call,
    )


@router.patch("/bugs/{bug_id}", response_model=BugListItem)
async def update_bug(bug_id: str, payload: BugUpdate, repo: BugRepository = Depends(get_repo)):
    bug = await repo.get_bug_by_id(bug_id)
    if bug is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")

    old_severity = bug.severity
    old_status = bug.status

    new_status = payload.status or bug.status
    if payload.status is not None:
        _validate_status_transition(bug.status, payload.status)

    updated = await repo.update_bug_admin(
        bug_id,
        severity=payload.severity,
        status=new_status if payload.status is not None else None,
        resolution_type=payload.resolution_type,
        closure_reason=payload.closure_reason,
        fix_provided=payload.fix_provided,
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")

    # Audit logging
    if payload.severity is not None and payload.severity != old_severity:
        await repo.create_audit_log(
            bug_id=bug_id, action="priority_updated", source="admin_panel",
            payload={"previous_severity": old_severity, "new_severity": payload.severity},
        )
    if payload.status == "resolved" and old_status != "resolved":
        audit_payload: dict = {"previous_status": old_status, "reason": "Resolved via admin panel"}
        if payload.resolution_type:
            audit_payload["resolution_type"] = payload.resolution_type
        if payload.closure_reason:
            audit_payload["closure_reason"] = payload.closure_reason
        if payload.fix_provided:
            audit_payload["fix_provided"] = payload.fix_provided
        await repo.create_audit_log(
            bug_id=bug_id, action="bug_closed", source="admin_panel",
            payload=audit_payload,
        )

    investigation = await repo.get_investigation(bug_id)
    investigation_summary = None
    tagged_on: list[TaggedOnEntry] = []
    current_on_call: list[TaggedOnEntry] = []
    if investigation is not None:
        investigation_summary = {
            "summary": investigation.summary,
            "fix_type": investigation.fix_type,
            "confidence": investigation.confidence,
        }
        as_of = updated.created_at.date() if updated.created_at else date.today()
        tagged_on = await _resolve_tagged_on(
            repo, investigation.relevant_services or [], as_of_date=as_of
        )
        current_on_call = await _resolve_tagged_on(
            repo, investigation.relevant_services or [], as_of_date=None
        )
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
        assignee_user_id=updated.assignee_user_id,
        resolution_type=updated.resolution_type,
        closure_reason=updated.closure_reason,
        fix_provided=updated.fix_provided,
        investigation_summary=investigation_summary,
        tagged_on=tagged_on,
        current_on_call=current_on_call,
    )


@router.get("/bugs/{bug_id}/investigation", response_model=InvestigationResponse)
async def get_investigation(bug_id: str, repo: BugRepository = Depends(get_repo)):
    investigation = await repo.get_investigation(bug_id)
    if investigation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Investigation not found")
    messages = await repo.get_investigation_messages(
        bug_id, investigation_id=str(investigation.id),
    )
    followups = await repo.get_followup_investigations(bug_id)
    followup_items = []
    for f in followups:
        f_messages = await repo.get_investigation_messages(bug_id, followup_id=str(f.id))
        followup_items.append(InvestigationFollowupResponse(
            id=str(f.id),
            bug_id=f.bug_id,
            trigger_state=f.trigger_state,
            action=f.action,
            fix_type=f.fix_type,
            summary=f.summary,
            confidence=f.confidence,
            root_cause=f.root_cause,
            pr_url=f.pr_url,
            recommended_actions=f.recommended_actions or [],
            relevant_services=f.relevant_services or [],
            cost_usd=f.cost_usd,
            duration_ms=f.duration_ms,
            messages=[
                InvestigationMessageResponse(
                    id=str(m.id), sequence=m.sequence, message_type=m.message_type,
                    content=m.content, created_at=m.created_at,
                )
                for m in f_messages
            ],
            created_at=f.created_at,
        ))
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
        messages=[
            InvestigationMessageResponse(
                id=str(m.id), sequence=m.sequence, message_type=m.message_type,
                content=m.content, created_at=m.created_at,
            )
            for m in messages
        ],
        followups=followup_items,
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


@router.post("/bugs/{bug_id}/nudge", response_model=NudgeResponse)
async def nudge_oncall(bug_id: str, repo: BugRepository = Depends(get_repo)):
    """Send a Slack DM nudge to each tagged on-call engineer for this bug."""
    bug = await repo.get_bug_by_id(bug_id)
    if bug is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")

    investigation = await repo.get_investigation(bug_id)
    tagged_on = await _resolve_tagged_on(
        repo, investigation.relevant_services if investigation else None,
    )
    if not tagged_on:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No on-call engineers tagged for this bug",
        )

    oncall_ids = list(dict.fromkeys(
        uid
        for entry in tagged_on
        if (uid := entry.oncall_engineer or entry.service_owner)
    ))
    if not oncall_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No on-call engineers tagged for this bug",
        )

    slack_url = _slack_message_url(bug.slack_channel_id, bug.slack_thread_ts)
    summary = investigation.summary if investigation else None

    results = await asyncio.gather(
        *(
            send_nudge(
                engineer_slack_id=uid,
                bug_id=bug.bug_id,
                severity=bug.severity,
                original_message=bug.original_message,
                slack_message_url=slack_url,
                summary=summary,
            )
            for uid in oncall_ids
        ),
        return_exceptions=True,
    )

    nudged: list[str] = []
    failed: list[str] = []
    errors: list[str] = []
    for uid, result in zip(oncall_ids, results):
        if isinstance(result, Exception):
            failed.append(uid)
            errors.append(f"{uid}: {result}")
        elif result is None:
            nudged.append(uid)
        else:
            failed.append(uid)
            errors.append(f"{uid}: {result}")

    message = f"Nudged {len(nudged)} of {len(oncall_ids)} on-call engineer(s)"
    if errors:
        message += f" — {'; '.join(errors)}"

    return NudgeResponse(
        bug_id=bug_id,
        nudged_users=nudged,
        failed_users=failed,
        message=message,
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


@router.get("/bugs/{bug_id}/audit-logs", response_model=AuditLogListResponse)
async def get_bug_audit_logs(bug_id: str, repo: BugRepository = Depends(get_repo)):
    try:
        bug = await repo.get_bug_by_id(bug_id)
        if bug is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")
        rows = await repo.get_audit_logs(bug_id)
        items = [
            AuditLogResponse(
                id=str(a.id),
                bug_id=a.bug_id,
                action=a.action,
                source=a.source,
                performed_by=a.performed_by,
                payload=a.payload if isinstance(a.payload, dict) else None,
                metadata=a.metadata_ if isinstance(a.metadata_, dict) else None,
                created_at=a.created_at,
            )
            for a in rows
        ]
        return AuditLogListResponse(items=items, total=len(items))
    except HTTPException:
        raise
    except ProgrammingError as e:
        if "bug_audit_logs" in str(e) and "does not exist" in str(e).lower():
            return AuditLogListResponse(items=[], total=0)
        logger.exception("get_bug_audit_logs failed for bug_id=%s", bug_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        ) from e
    except Exception as e:
        logger.exception("get_bug_audit_logs failed for bug_id=%s", bug_id)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        ) from e


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


@router.get("/bugs/{bug_id}/followups", response_model=InvestigationFollowupListResponse)
async def get_bug_followups(bug_id: str, repo: BugRepository = Depends(get_repo)):
    bug = await repo.get_bug_by_id(bug_id)
    if bug is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bug not found")
    followups = await repo.get_followup_investigations(bug_id)
    items = []
    for f in followups:
        messages = await repo.get_investigation_messages(bug_id, followup_id=str(f.id))
        items.append(InvestigationFollowupResponse(
            id=str(f.id),
            bug_id=f.bug_id,
            trigger_state=f.trigger_state,
            action=f.action,
            fix_type=f.fix_type,
            summary=f.summary,
            confidence=f.confidence,
            root_cause=f.root_cause,
            pr_url=f.pr_url,
            recommended_actions=f.recommended_actions or [],
            relevant_services=f.relevant_services or [],
            cost_usd=f.cost_usd,
            duration_ms=f.duration_ms,
            messages=[
                InvestigationMessageResponse(
                    id=str(m.id), sequence=m.sequence, message_type=m.message_type,
                    content=m.content, created_at=m.created_at,
                )
                for m in messages
            ],
            created_at=f.created_at,
        ))
    return InvestigationFollowupListResponse(items=items)


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
    is_active: bool = Query(True, description="Filter by active status"),
    team_id: str | None = Query(default=None, description="Filter by team ID"),
    tier: str | None = Query(default=None, description="Filter by tier"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    items, total = await repo.list_service_mappings(
        service_name=service_name,
        tech_stack=tech_stack,
        is_active=is_active,
        team_id=team_id,
        tier=tier,
        page=page,
        page_size=page_size,
    )
    result_items = [_mapping_response(m) for m in items]
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
            name=m.team.name,
            oncall_engineer=m.team.oncall_engineer,
        )
    return ServiceTeamMappingResponse(
        id=str(m.id),
        service_name=m.service_name,
        github_repo=m.github_repo,
        team_slack_group=m.team_slack_group,
        primary_oncall=m.primary_oncall,
        tech_stack=m.tech_stack,
        description=m.description,
        service_owner=m.service_owner,
        team_id=str(m.team_id) if m.team_id else None,
        repository_url=m.repository_url,
        environment=m.environment,
        tier=m.tier,
        metadata=m.metadata_,
        is_active=m.is_active,
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


async def _enrich_preview_entries(entries: list[dict]) -> list[dict]:
    """Resolve Slack user IDs to display names for preview entries."""
    unique_ids = {e["engineer_slack_id"] for e in entries if e.get("engineer_slack_id")}
    if not unique_ids:
        return entries
    import asyncio
    infos = await asyncio.gather(*(get_user_info(uid) for uid in unique_ids), return_exceptions=True)
    name_map = {}
    for uid, info in zip(unique_ids, infos):
        if isinstance(info, dict) and info:
            name_map[uid] = info.get("display_name") or info.get("real_name") or uid
        else:
            name_map[uid] = uid
    for e in entries:
        e["engineer_display_name"] = name_map.get(e.get("engineer_slack_id", ""))
    return entries


def _team_response(t) -> TeamResponse:
    return TeamResponse(
        id=str(t.id),
        slack_group_id=t.slack_group_id,
        name=t.name,
        slug=t.slug,
        description=t.description,
        slack_channel_id=t.slack_channel_id,
        oncall_engineer=t.oncall_engineer,
        rotation_enabled=t.rotation_enabled,
        rotation_type=t.rotation_type,
        rotation_order=t.rotation_order,
        rotation_start_date=t.rotation_start_date,
        current_rotation_index=t.current_rotation_index,
        rotation_interval=t.rotation_interval,
        handoff_day=t.handoff_day,
        handoff_time=t.handoff_time,
        is_active=t.is_active,
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


@router.get("/teams", response_model=PaginatedTeams)
async def list_teams(
    *,
    repo: BugRepository = Depends(get_repo),
    is_active: bool = Query(True, description="Filter by active status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    items, total = await repo.list_teams(is_active=is_active, page=page, page_size=page_size)
    result_items = [_team_response(t) for t in items]
    return PaginatedTeams(items=result_items, total=total, page=page, page_size=page_size)


@router.post("/teams", status_code=status.HTTP_201_CREATED, response_model=TeamResponse)
async def create_team(
    payload: TeamCreate,
    repo: BugRepository = Depends(get_repo),
):
    data = payload.model_dump()
    t = await repo.create_team(data)
    return _team_response(t)


@router.get("/teams/{id}", response_model=TeamResponse)
async def get_team(id: str, repo: BugRepository = Depends(get_repo)):
    t = await repo.get_team_by_id(id)
    if t is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    return _team_response(t)


@router.patch("/teams/{id}", response_model=TeamResponse)
async def update_team(
    id: str,
    payload: TeamUpdate,
    repo: BugRepository = Depends(get_repo),
):
    old_team = await repo.get_team_by_id(id)
    if old_team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")
    old_oncall = old_team.oncall_engineer

    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    t = await repo.update_team(id, data)
    if t is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    # Log history if oncall_engineer changed
    if "oncall_engineer" in data and data["oncall_engineer"] != old_oncall:
        await repo.log_oncall_change(
            team_id=id,
            engineer_slack_id=data["oncall_engineer"],
            change_type="manual",
            effective_date=date.today(),
            previous_engineer_slack_id=old_oncall,
            change_reason="Manual assignment via admin panel",
        )

    # Audit log for team updates
    await repo.create_oncall_audit_log(
        entity_type="team",
        entity_id=id,
        action="updated",
        team_id=id,
        changes={k: {"old": getattr(old_team, k, None), "new": v} for k, v in data.items()},
    )

    return _team_response(t)


@router.delete("/teams/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(id: str, repo: BugRepository = Depends(get_repo)):
    await repo.create_oncall_audit_log(
        entity_type="team",
        entity_id=id,
        action="deleted",
        team_id=id,
        actor_type="user",
    )
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
            origin=s.origin,
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
        origin=schedule.origin,
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
        origin=schedule.origin,
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
        origin=updated.origin,
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


# --- On-Call Overrides ---


@router.post(
    "/teams/{team_id}/oncall-overrides",
    status_code=status.HTTP_201_CREATED,
    response_model=OnCallOverrideResponse,
)
async def create_oncall_override(
    team_id: str,
    payload: OnCallOverrideCreate,
    repo: BugRepository = Depends(get_repo),
):
    """Create a date-specific on-call override for a team."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    # Validate dates
    if payload.end_date is not None and payload.end_date < payload.override_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="end_date must be on or after override_date",
        )

    # Check for overlapping overrides
    has_overlap = await repo.check_override_overlap(
        team_id, payload.override_date, payload.end_date
    )
    if has_overlap:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Override overlaps with an existing override for this team",
        )

    # Auto-populate original_engineer if not provided
    original_engineer = payload.original_engineer_slack_id
    if not original_engineer:
        current = await oncall_service.get_current_oncall(repo, team_id, payload.override_date)
        if current:
            original_engineer = current.get("engineer_slack_id")

    override = await repo.create_oncall_override(
        team_id=team_id,
        data={
            "override_date": payload.override_date,
            "end_date": payload.end_date,
            "substitute_engineer_slack_id": payload.substitute_engineer_slack_id,
            "original_engineer_slack_id": original_engineer,
            "reason": payload.reason,
            "created_by": "ADMIN",
            "status": "approved",
            "requested_by": "ADMIN",
        },
    )

    # Log to history
    await repo.log_oncall_change(
        team_id=team_id,
        engineer_slack_id=payload.substitute_engineer_slack_id,
        change_type="override_created",
        effective_date=payload.override_date,
        previous_engineer_slack_id=original_engineer,
        change_reason=payload.reason,
    )

    return OnCallOverrideResponse(
        id=str(override.id),
        team_id=str(override.team_id),
        override_date=override.override_date,
        end_date=override.end_date,
        substitute_engineer_slack_id=override.substitute_engineer_slack_id,
        original_engineer_slack_id=override.original_engineer_slack_id,
        reason=override.reason,
        status=override.status,
        requested_by=override.requested_by,
        approved_by=override.approved_by,
        created_by=override.created_by,
        created_at=override.created_at,
    )


@router.get(
    "/teams/{team_id}/oncall-overrides",
    response_model=PaginatedOnCallOverrides,
)
async def list_oncall_overrides(
    team_id: str,
    repo: BugRepository = Depends(get_repo),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """List on-call overrides for a team."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    items, total = await repo.list_oncall_overrides(
        team_id=team_id,
        page=page,
        page_size=page_size,
    )
    result_items = [
        OnCallOverrideResponse(
            id=str(o.id),
            team_id=str(o.team_id),
            override_date=o.override_date,
            end_date=o.end_date,
            substitute_engineer_slack_id=o.substitute_engineer_slack_id,
            original_engineer_slack_id=o.original_engineer_slack_id,
            reason=o.reason,
            status=o.status,
            requested_by=o.requested_by,
            approved_by=o.approved_by,
            created_by=o.created_by,
            created_at=o.created_at,
        )
        for o in items
    ]
    return PaginatedOnCallOverrides(
        items=result_items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.delete(
    "/teams/{team_id}/oncall-overrides/{override_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_oncall_override(
    team_id: str,
    override_id: str,
    repo: BugRepository = Depends(get_repo),
):
    """Delete an on-call override."""
    override = await repo.get_oncall_override_by_id(override_id)
    if override is None or str(override.team_id) != team_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Override not found")
    await repo.delete_oncall_override(override_id)

    # Log to history
    await repo.log_oncall_change(
        team_id=team_id,
        engineer_slack_id=override.substitute_engineer_slack_id,
        change_type="override_deleted",
        effective_date=override.override_date,
        previous_engineer_slack_id=override.original_engineer_slack_id,
        change_reason=f"Override deleted: {override.reason}",
    )

    return None


# --- Override Approval ---


@router.patch(
    "/teams/{team_id}/oncall-overrides/{override_id}",
    response_model=OnCallOverrideResponse,
)
async def update_oncall_override_status(
    team_id: str,
    override_id: str,
    payload: OverrideStatusUpdate,
    repo: BugRepository = Depends(get_repo),
):
    """Approve, reject, or cancel an on-call override."""
    override = await repo.get_oncall_override_by_id(override_id)
    if override is None or str(override.team_id) != team_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Override not found")

    # Validate state transitions
    valid_transitions = {
        "pending": {"approved", "rejected"},
        "approved": {"cancelled"},
    }
    allowed = valid_transitions.get(override.status, set())
    if payload.status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid transition from '{override.status}' to '{payload.status}'",
        )

    if payload.status == "approved":
        updated = await oncall_service.approve_override(repo, override_id, payload.approved_by or "ADMIN")
    elif payload.status == "rejected":
        updated = await oncall_service.reject_override(repo, override_id, payload.approved_by or "ADMIN")
    else:
        updated = await repo.update_oncall_override(override_id, {"status": "cancelled"})

    if updated is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Could not update override")

    return OnCallOverrideResponse(
        id=str(updated.id),
        team_id=str(updated.team_id),
        override_date=updated.override_date,
        end_date=updated.end_date,
        substitute_engineer_slack_id=updated.substitute_engineer_slack_id,
        original_engineer_slack_id=updated.original_engineer_slack_id,
        reason=updated.reason,
        status=updated.status,
        requested_by=updated.requested_by,
        approved_by=updated.approved_by,
        created_by=updated.created_by,
        created_at=updated.created_at,
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
    old_values = {k: getattr(team, k, None) for k in data}
    updated = await repo.update_team(team_id, data)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    # Audit log — convert non-serializable values (date, time) to strings
    def _serialize(val):
        if isinstance(val, (date, time)):
            return val.isoformat()
        return val

    await repo.create_oncall_audit_log(
        entity_type="rotation_config",
        entity_id=team_id,
        action="updated",
        team_id=team_id,
        changes={k: {"old": _serialize(old_values.get(k)), "new": _serialize(v)} for k, v in data.items()},
    )

    # If rotation config changed, delete future auto schedules and regenerate
    config_fields = {"rotation_type", "rotation_interval", "handoff_day", "rotation_order", "rotation_start_date"}
    if data.keys() & config_fields and updated.rotation_enabled:
        await repo.delete_future_auto_schedules(team_id)
        try:
            await oncall_service.generate_schedules(repo, team_id, weeks=4)
        except Exception:
            logger.warning("Failed to regenerate lookahead schedules for team %s", team_id)

    return _team_response(updated)


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

    # if not team.rotation_enabled:
    #     raise HTTPException(
    #         status_code=status.HTTP_400_BAD_REQUEST,
    #         detail="Rotation is not enabled for this team",
    #     )

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

    return _team_response(updated)


# --- Team Members ---


@router.get(
    "/teams/{team_id}/members",
    response_model=list[TeamMembershipResponse],
)
async def list_team_members(
    team_id: str,
    repo: BugRepository = Depends(get_repo),
):
    """List team members (merged Slack group + DB metadata)."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    merged = await repo.merge_slack_members_with_db(team_id, team.slack_group_id)
    return [
        TeamMembershipResponse(
            id=str(m["id"]) if m.get("id") else None,
            team_id=team_id,
            slack_user_id=m["slack_user_id"],
            team_role=m.get("team_role", "member"),
            is_eligible_for_oncall=m.get("is_eligible_for_oncall", True),
            weight=m.get("weight", 1.0),
            joined_at=m.get("joined_at"),
            display_name=m.get("display_name"),
            in_db=m.get("in_db", False),
        )
        for m in merged
    ]


@router.post(
    "/teams/{team_id}/members",
    status_code=status.HTTP_201_CREATED,
    response_model=TeamMembershipResponse,
)
async def upsert_team_member(
    team_id: str,
    payload: TeamMembershipUpsert,
    repo: BugRepository = Depends(get_repo),
):
    """Create or update a team member's oncall metadata."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    data = {k: v for k, v in payload.model_dump().items() if v is not None and k != "slack_user_id"}
    m = await repo.upsert_team_membership(team_id, payload.slack_user_id, data)

    await repo.create_oncall_audit_log(
        entity_type="team_membership",
        entity_id=str(m.id),
        action="upserted",
        team_id=team_id,
        changes=data,
    )

    return TeamMembershipResponse(
        id=str(m.id),
        team_id=str(m.team_id),
        slack_user_id=m.slack_user_id,
        team_role=m.team_role,
        is_eligible_for_oncall=m.is_eligible_for_oncall,
        weight=m.weight,
        joined_at=m.joined_at,
        in_db=True,
    )


@router.patch(
    "/teams/{team_id}/members/{slack_user_id}",
    response_model=TeamMembershipResponse,
)
async def update_team_member(
    team_id: str,
    slack_user_id: str,
    payload: TeamMembershipUpdate,
    repo: BugRepository = Depends(get_repo),
):
    """Update a team member's role, weight, or eligibility."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No fields to update")

    m = await repo.upsert_team_membership(team_id, slack_user_id, data)

    await repo.create_oncall_audit_log(
        entity_type="team_membership",
        entity_id=str(m.id),
        action="updated",
        team_id=team_id,
        changes=data,
    )

    return TeamMembershipResponse(
        id=str(m.id),
        team_id=str(m.team_id),
        slack_user_id=m.slack_user_id,
        team_role=m.team_role,
        is_eligible_for_oncall=m.is_eligible_for_oncall,
        weight=m.weight,
        joined_at=m.joined_at,
        in_db=True,
    )


@router.delete(
    "/teams/{team_id}/members/{slack_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_team_member(
    team_id: str,
    slack_user_id: str,
    repo: BugRepository = Depends(get_repo),
):
    """Remove a team member's oncall metadata."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    await repo.delete_team_membership(team_id, slack_user_id)

    await repo.create_oncall_audit_log(
        entity_type="team_membership",
        entity_id=slack_user_id,
        action="deleted",
        team_id=team_id,
    )

    return None


# --- Rotation Preview & Generate ---


@router.post(
    "/teams/{team_id}/rotation/preview",
    response_model=RotationPreviewResponse,
)
async def preview_rotation(
    team_id: str,
    payload: RotationGenerateRequest | None = None,
    repo: BugRepository = Depends(get_repo),
):
    """Simulate rotation for the next N weeks without persisting."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    # if not team.rotation_enabled:
    #     raise HTTPException(
    #         status_code=status.HTTP_400_BAD_REQUEST,
    #         detail="Rotation is not enabled for this team",
    #     )

    weeks = payload.weeks if payload else 4
    entries = await oncall_service.preview_rotation(repo, team_id, weeks)
    entries = await _enrich_preview_entries(entries)
    return RotationPreviewResponse(
        items=[RotationPreviewEntry(**e) for e in entries]
    )


@router.post(
    "/teams/{team_id}/rotation/generate",
    response_model=RotationPreviewResponse,
)
async def generate_rotation_schedules(
    team_id: str,
    payload: RotationGenerateRequest | None = None,
    repo: BugRepository = Depends(get_repo),
):
    """Force-generate auto schedules for the next N weeks."""
    team = await repo.get_team_by_id(team_id)
    if team is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found")

    weeks = payload.weeks if payload else 4
    entries = await oncall_service.generate_schedules(repo, team_id, weeks)
    entries = await _enrich_preview_entries(entries)
    return RotationPreviewResponse(
        items=[RotationPreviewEntry(**e) for e in entries]
    )


# --- OnCall Audit Logs ---


@router.get(
    "/oncall-audit-logs",
    response_model=PaginatedOnCallAuditLogs,
)
async def list_oncall_audit_logs(
    *,
    repo: BugRepository = Depends(get_repo),
    entity_type: str | None = Query(default=None),
    entity_id: str | None = Query(default=None),
    action: str | None = Query(default=None),
    actor_id: str | None = Query(default=None),
    team_id: str | None = Query(default=None),
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """List on-call audit logs with filtering."""
    items, total = await repo.list_oncall_audit_logs(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        actor_id=actor_id,
        team_id=team_id,
        from_date=from_date,
        to_date=to_date,
        page=page,
        page_size=page_size,
    )
    result_items = [
        OnCallAuditLogResponse(
            id=str(a.id),
            team_id=str(a.team_id) if a.team_id else None,
            entity_type=a.entity_type,
            entity_id=str(a.entity_id),
            action=a.action,
            actor_type=a.actor_type,
            actor_id=a.actor_id,
            changes=a.changes,
            metadata=a.metadata_,
            created_at=a.created_at,
        )
        for a in items
    ]
    return PaginatedOnCallAuditLogs(
        items=result_items,
        total=total,
        page=page,
        page_size=page_size,
    )


# --- Global On-Call Lookup ---


@router.get(
    "/oncall",
    response_model=list[GlobalOnCallResponse],
)
async def global_oncall_lookup(
    *,
    repo: BugRepository = Depends(get_repo),
    service: str | None = Query(default=None, description="Service name to look up"),
    team: str | None = Query(default=None, description="Team name or slug to look up"),
):
    """Who is on-call? Look up by service or team name."""
    results = await repo.global_oncall_lookup(service_name=service, team_name=team)
    return [GlobalOnCallResponse(**r) for r in results]


@router.get(
    "/services/{service_id}/oncall",
    response_model=GlobalOnCallResponse,
)
async def get_service_oncall(
    service_id: str,
    repo: BugRepository = Depends(get_repo),
):
    """Get the current on-call engineer for a specific service."""
    result = await repo.get_service_oncall(service_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found or no on-call configured")
    return GlobalOnCallResponse(**result)


@router.get(
    "/users/{slack_id}/schedules",
    response_model=list[OnCallScheduleResponse],
)
async def get_user_schedules(
    slack_id: str,
    from_date: date | None = Query(default=None),
    to_date: date | None = Query(default=None),
    repo: BugRepository = Depends(get_repo),
):
    """Get all on-call schedules for a user across all teams."""
    schedules = await repo.get_user_schedules(slack_id, from_date, to_date)
    return [
        OnCallScheduleResponse(
            id=str(s.id),
            team_id=str(s.team_id),
            engineer_slack_id=s.engineer_slack_id,
            start_date=s.start_date,
            end_date=s.end_date,
            schedule_type=s.schedule_type,
            days_of_week=s.days_of_week,
            origin=s.origin,
            created_by=s.created_by,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in schedules
    ]


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


# --- RAG Chat ---


class ChatMessageItem(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    conversation_history: list[ChatMessageItem] = []


class ChatSourceItem(BaseModel):
    bug_id: str
    source_type: str
    chunk_text: str
    similarity: float
    link: str | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[ChatSourceItem]


class RagIndexResponse(BaseModel):
    status: str
    total: int
    indexed: dict


class RagStatsResponse(BaseModel):
    total_documents: int
    by_type: dict
    last_indexed_at: str | None


@router.post("/chat", response_model=ChatResponse)
async def chat(payload: ChatRequest):
    """RAG-powered chat endpoint — answers questions about bugs and investigations."""
    from bug_bot.rag.chat import rag_chat

    async with async_session() as session:
        try:
            history = [{"role": m.role, "content": m.content} for m in payload.conversation_history]
            result = await rag_chat(session, payload.message, history)
        except Exception as e:
            logger.exception("RAG chat error")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Chat error: {e}",
            )
    return ChatResponse(
        answer=result["answer"],
        sources=[ChatSourceItem(**s) for s in result["sources"]],
    )


@router.post("/rag/index", response_model=RagIndexResponse)
async def rag_reindex():
    """Trigger a full re-index of all bugs, investigations, and findings."""
    from bug_bot.rag.indexer import reindex_all

    async with async_session() as session:
        try:
            result = await reindex_all(session)
        except Exception as e:
            logger.exception("RAG reindex error")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Reindex error: {e}",
            )
    return RagIndexResponse(status="completed", total=result["total"], indexed=result["indexed"])


@router.get("/rag/stats", response_model=RagStatsResponse)
async def rag_stats():
    """Return RAG index statistics."""
    from bug_bot.rag.vectorstore import get_stats

    async with async_session() as session:
        stats = await get_stats(session)
    return RagStatsResponse(**stats)

