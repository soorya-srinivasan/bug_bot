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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    register_handlers(slack_app)
    yield
    # Shutdown


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

    # Save to DB
    async with async_session() as session:
        repo = BugRepository(session)
        await repo.create_bug_report(
            bug_id=bug_id,
            channel_id="local-test",
            thread_ts="0",
            reporter=payload.reporter,
            message=payload.message,
            workflow_id=workflow_id,
        )

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
