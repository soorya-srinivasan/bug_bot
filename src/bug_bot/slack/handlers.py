from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from bug_bot.config import settings
from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository
from bug_bot.temporal.client import get_temporal_client
from bug_bot.temporal import BugReportInput
from bug_bot.temporal.workflows.bug_investigation import BugInvestigationWorkflow


def register_handlers(app: AsyncApp):

    @app.event("message")
    async def handle_message(event: dict, client: AsyncWebClient):
        if event.get("channel") != settings.bug_reports_channel_id:
            return
        if event.get("bot_id") or event.get("thread_ts") or event.get("subtype"):
            return

        channel_id = event["channel"]
        thread_ts = event["ts"]
        reporter = event.get("user", "unknown")
        text = event.get("text", "")
        bug_id = f"BUG-{int(float(thread_ts))}"
        workflow_id = f"bug-{thread_ts.replace('.', '-')}"

        # Acknowledge immediately
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=(
                f":mag: *Bug Bot* received this report (`{bug_id}`).\n"
                f"I'm starting an investigation and will update this thread.\n"
                f"If I need human help, I'll tag the relevant team.\n"
                f"_Report filed by <@{reporter}>_"
            ),
        )

        # Save to DB
        async with async_session() as session:
            repo = BugRepository(session)
            await repo.create_bug_report(
                bug_id=bug_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                reporter=reporter,
                message=text,
                workflow_id=workflow_id,
            )

        # Start Temporal workflow
        temporal = await get_temporal_client()
        await temporal.start_workflow(
            BugInvestigationWorkflow.run,
            BugReportInput(
                bug_id=bug_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_text=text,
                reporter_user_id=reporter,
            ),
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
        )

    # Handle !resolve / !close commands in threads
    @app.message(r"!resolve|!close|!fixed")
    async def handle_resolution(event: dict, client: AsyncWebClient):
        if not event.get("thread_ts"):
            return

        thread_ts = event["thread_ts"]
        bug_id = f"BUG-{int(float(thread_ts))}"

        temporal = await get_temporal_client()
        try:
            handle = temporal.get_workflow_handle(f"sla-{bug_id}")
            await handle.signal("mark_resolved")
        except Exception:
            pass  # SLA workflow may not exist

        # Update DB
        async with async_session() as session:
            repo = BugRepository(session)
            await repo.update_status(bug_id, "resolved")

        await client.chat_postMessage(
            channel=event["channel"],
            thread_ts=thread_ts,
            text=":white_check_mark: Bug marked as resolved. SLA tracking stopped.",
        )
