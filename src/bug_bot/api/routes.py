from fastapi import APIRouter, HTTPException

from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository
from bug_bot.temporal.client import get_temporal_client
from bug_bot.temporal.workflows.bug_investigation import BugInvestigationWorkflow

router = APIRouter(prefix="/api")


@router.post("/resolve-bug/{bug_id}")
async def resolve_bug(bug_id: str):
    """
    Programmatically resolve a bug. Signals the main investigation workflow
    (so it can do proper cleanup) and the SLA workflow. Also updates DB status.
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
        await repo.update_status(bug_id, "resolved")
        await repo.log_conversation(
            bug_id=bug_id,
            message_type="resolved",
            sender_type="system",
            sender_id="api",
            message_text="Resolved via API call",
        )

    return {"status": "resolved", "bug_id": bug_id, "workflow_signaled": workflow_signaled}
