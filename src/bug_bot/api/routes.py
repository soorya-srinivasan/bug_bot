from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository
from bug_bot.temporal.client import get_temporal_client
from bug_bot.temporal.workflows.bug_investigation import BugInvestigationWorkflow

router = APIRouter(prefix="/api")


class ResolveBugRequest(BaseModel):
    resolution_type: Literal["code_fix", "data_fix", "sre_fix", "not_a_valid_bug"] | None = None
    closure_reason: str | None = None
    fix_provided: str | None = None


@router.post("/resolve-bug/{bug_id}")
async def resolve_bug(bug_id: str, body: ResolveBugRequest | None = None):
    """
    Programmatically resolve a bug. Signals the main investigation workflow
    (so it can do proper cleanup) and the SLA workflow. Also updates DB status.
    Optionally accepts resolution_type, closure_reason, fix_provided.
    Returns 404 if bug not found, 409 if already resolved.
    """
    async with async_session() as session:
        repo = BugRepository(session)
        bug = await repo.get_bug_by_id(bug_id)
        if not bug:
            raise HTTPException(status_code=404, detail=f"Bug {bug_id} not found")
        if bug.status == "resolved":
            raise HTTPException(status_code=409, detail=f"Bug {bug_id} is already resolved")

    temporal = await get_temporal_client()
    workflow_signaled = False

    # Signal the main investigation workflow so it can clean up workspace etc.
    if bug.temporal_workflow_id:
        try:
            handle = temporal.get_workflow_handle(bug.temporal_workflow_id)
            await handle.signal(BugInvestigationWorkflow.close_requested)
            workflow_signaled = True
        except Exception:
            pass  # Workflow may have already completed; fall through to DB update

    # Signal SLA workflow (best-effort)
    try:
        sla_handle = temporal.get_workflow_handle(f"sla-{bug_id}")
        await sla_handle.signal("mark_resolved")
    except Exception:
        pass

    # Update DB and log (handles case where workflow already ended)
    async with async_session() as session:
        repo = BugRepository(session)

        # Save resolution details if provided
        if body and (body.resolution_type or body.closure_reason or body.fix_provided):
            await repo.update_resolution_details(
                bug_id,
                resolution_type=body.resolution_type or "code_fix",
                closure_reason=body.closure_reason or "Resolved via API",
                fix_provided=body.fix_provided,
            )

        await repo.update_status(bug_id, "resolved")
        await repo.log_conversation(
            bug_id=bug_id,
            message_type="resolved",
            sender_type="system",
            sender_id="api",
            message_text="Resolved via API call",
        )
        audit_payload: dict = {"previous_status": bug.status, "reason": "Resolved via API call"}
        if body:
            if body.resolution_type:
                audit_payload["resolution_type"] = body.resolution_type
            if body.closure_reason:
                audit_payload["closure_reason"] = body.closure_reason
            if body.fix_provided:
                audit_payload["fix_provided"] = body.fix_provided
        await repo.create_audit_log(
            bug_id=bug_id, action="bug_closed", source="api",
            payload=audit_payload,
        )

    return {"status": "resolved", "bug_id": bug_id, "workflow_signaled": workflow_signaled}
