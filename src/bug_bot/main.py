import asyncio
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from pydantic import BaseModel

from bug_bot.config import settings
from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository
from bug_bot.temporal.client import get_temporal_client
from bug_bot.temporal import BugReportInput
from bug_bot.temporal.workflows.bug_investigation import BugInvestigationWorkflow
from bug_bot.slack.app import slack_app, slack_handler
from bug_bot.slack.handlers import register_handlers
from bug_bot.triage import triage_bug_report

logger = logging.getLogger(__name__)

_APPROVE_RE = re.compile(
    r'\b(go ahead|lgtm|create pr|yes|approved|ship it|approve|do it|proceed|looks good)\b',
    re.IGNORECASE,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    register_handlers(slack_app)

    if settings.slack_socket_mode:
        print("Starting Slack in Socket Mode...")
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        socket_handler = AsyncSocketModeHandler(slack_app, settings.slack_app_token)
        socket_task = asyncio.create_task(socket_handler.start_async())
        logger.info("Slack Socket Mode started")
        yield
        socket_task.cancel()
        try:
            await socket_task
        except asyncio.CancelledError:
            pass
    else:
        print("Slack HTTP Mode — ensure your server is accessible at the URL configured in Slack App settings")
        logger.info("Slack HTTP mode — expecting events at /slack/events")
        yield


app = FastAPI(title="Bug Bot", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/slack/events")
async def slack_events(request: Request):
    return await slack_handler.handle(request)


# --- Local testing endpoint (no Slack required) ---


class BugReportRequest(BaseModel):
    bug_id: str
    message: str
    reporter: str = "local-tester"


@app.post("/api/report-bug")
async def report_bug(payload: BugReportRequest):
    """Submit a bug report for investigation (local testing, no Slack needed)."""
    bug_id = payload.bug_id
    workflow_id = f"bug-{bug_id}"

    # Run triage classification
    triage = await triage_bug_report(payload.message, payload.reporter)
    severity = triage.get("severity", "P3")

    # Save to DB
    async with async_session() as session:
        repo = BugRepository(session)
        await repo.create_bug_report(
            bug_id=bug_id,
            channel_id="local-test",
            thread_ts="0",
            reporter=payload.reporter,
            message=payload.message,
            severity=severity,
            status="triaged",
            workflow_id=workflow_id,
        )

    # Skip investigation for noise
    if not triage.get("needs_investigation", True):
        return {
            "status": "skipped",
            "bug_id": bug_id,
            "triage": triage,
            "message": "Triage determined no investigation needed.",
        }

    # Start Temporal workflow
    temporal = await get_temporal_client()
    await temporal.start_workflow(
        BugInvestigationWorkflow.run,
        BugReportInput(
            bug_id=bug_id,
            channel_id="local-test",
            thread_ts="0",
            message_text=payload.message,
            reporter_user_id=payload.reporter,
        ),
        id=workflow_id,
        task_queue=settings.temporal_task_queue,
    )

    return {
        "status": "accepted",
        "bug_id": bug_id,
        "workflow_id": workflow_id,
        "triage": triage,
        "message": "Investigation started. Check Temporal UI at http://localhost:8080",
    }


@app.post("/api/resolve-bug/{bug_id}")
async def resolve_bug(bug_id: str):
    """Mark a bug as resolved (local testing, no Slack needed)."""
    temporal = await get_temporal_client()
    try:
        handle = temporal.get_workflow_handle(f"sla-{bug_id}")
        await handle.signal("mark_resolved")
    except Exception:
        pass

    async with async_session() as session:
        repo = BugRepository(session)
        await repo.update_status(bug_id, "resolved")

    return {"status": "resolved", "bug_id": bug_id}


@app.post("/api/triage")
async def triage_only(payload: BugReportRequest):
    """Run triage classification without starting an investigation."""
    triage = await triage_bug_report(payload.message, payload.reporter)
    return {"bug_id": payload.bug_id, "triage": triage}


class DevReplyRequest(BaseModel):
    message: str


@app.post("/api/dev-reply/{bug_id}")
async def dev_reply(bug_id: str, payload: DevReplyRequest):
    """Signal the Bug Bot workflow with a developer reply (local testing)."""
    intent = "approve" if _APPROVE_RE.search(payload.message) else "context"
    workflow_id = f"bug-{bug_id}"
    temporal = await get_temporal_client()
    handle = temporal.get_workflow_handle(workflow_id)
    try:
        await handle.signal(BugInvestigationWorkflow.dev_reply, payload.message, intent)
    except Exception as e:
        return {"status": "error", "bug_id": bug_id, "error": str(e)}
    return {"status": "signaled", "bug_id": bug_id, "intent": intent, "workflow_id": workflow_id}


@app.get("/api/bug/{bug_id}")
async def get_bug(bug_id: str):
    """Retrieve bug report and investigation details."""
    async with async_session() as session:
        repo = BugRepository(session)
        investigation = await repo.get_investigation(bug_id)
        if not investigation:
            return {"error": "not_found", "bug_id": bug_id}
        return {
            "bug_id": bug_id,
            "summary": investigation.summary,
            "root_cause": investigation.root_cause,
            "fix_type": investigation.fix_type,
            "confidence": investigation.confidence,
            "pr_url": investigation.pr_url,
            "relevant_services": investigation.relevant_services,
            "recommended_actions": investigation.recommended_actions,
            "conversation_history": investigation.conversation_history,
        }
