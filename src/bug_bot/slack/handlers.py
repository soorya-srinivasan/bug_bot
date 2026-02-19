import logging
import re

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from bug_bot.config import settings
from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository
from bug_bot.slack.messages import format_triage_response
from bug_bot.temporal.client import get_temporal_client
from bug_bot.temporal import BugReportInput
from bug_bot.temporal.workflows.bug_investigation import BugInvestigationWorkflow
from bug_bot.triage import triage_bug_report

logger = logging.getLogger(__name__)

_APPROVE_RE = re.compile(
    r'\b(go ahead|lgtm|create pr|yes|approved|ship it|approve|do it|proceed|looks good)\b',
    re.IGNORECASE,
)


def _detect_intent(text: str) -> str:
    """Return 'approve' if the dev wants a PR created, otherwise 'context'."""
    return "approve" if _APPROVE_RE.search(text) else "context"


def register_handlers(app: AsyncApp):

    @app.event("message")
    async def handle_message(event: dict, client: AsyncWebClient):
        channel_id = event.get("channel")
        if event.get("bot_id") or event.get("subtype"):
            return

        # --- Thread replies in #bug-reports ---
        if (
            channel_id == settings.bug_reports_channel_id
            and event.get("thread_ts")
            and event["thread_ts"] != event.get("ts")
        ):
            await _handle_bug_thread_reply(event, client)
            return

        # --- Thread replies in #bug-summaries ---
        if (
            channel_id == settings.bug_summaries_channel_id
            and event.get("thread_ts")
            and event["thread_ts"] != event.get("ts")
        ):
            await _handle_summary_thread_reply(event, client)
            return

        # --- New top-level message in #bug-reports ---
        if channel_id != settings.bug_reports_channel_id:
            return
        if event.get("thread_ts"):
            return

        thread_ts = event["ts"]
        reporter = event.get("user", "unknown")
        text = event.get("text", "")
        bug_id = f"BUG-{int(float(thread_ts))}"
        workflow_id = f"bug-{thread_ts.replace('.', '-')}"

        # Run triage classification
        triage = await triage_bug_report(text, reporter)
        severity = triage.get("severity", "P3")

        # Acknowledge with triage info
        ack_text = format_triage_response(triage, bug_id)
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=ack_text,
        )

        # Save to DB with triaged severity
        async with async_session() as session:
            repo = BugRepository(session)
            await repo.create_bug_report(
                bug_id=bug_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                reporter=reporter,
                message=text,
                severity=severity,
                status="triaged",
                workflow_id=workflow_id,
            )

        # Skip investigation for noise
        if not triage.get("needs_investigation", True):
            logger.info("Triage says no investigation needed for %s", bug_id)
            return

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


async def _handle_bug_thread_reply(event: dict, client: AsyncWebClient):
    """Handle a human reply in a #bug-reports thread — signal the workflow."""
    channel_id = event["channel"]
    thread_ts = event["thread_ts"]
    user = event.get("user", "unknown")
    text = event.get("text", "")

    async with async_session() as session:
        repo = BugRepository(session)
        bug = await repo.get_bug_by_thread_ts(channel_id, thread_ts)
        if not bug or not bug.temporal_workflow_id:
            return

    intent = _detect_intent(text)
    try:
        temporal = await get_temporal_client()
        handle = temporal.get_workflow_handle(bug.temporal_workflow_id)
        await handle.signal(BugInvestigationWorkflow.dev_reply, text, intent)
        ack = (
            ":robot_face: Got it! Creating a PR now..."
            if intent == "approve"
            else ":mag: Running a follow-up investigation with your additional context..."
        )
        await client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=ack)
    except Exception:
        logger.exception("Failed to signal workflow for %s", bug.bug_id)


async def _handle_summary_thread_reply(event: dict, client: AsyncWebClient):
    """Handle a reply in #bug-summaries — signal the workflow and relay to original thread."""
    thread_ts = event["thread_ts"]
    user = event.get("user", "unknown")
    text = event.get("text", "")

    async with async_session() as session:
        repo = BugRepository(session)
        bug = await repo.get_bug_by_summary_thread_ts(thread_ts)
        if not bug or not bug.temporal_workflow_id:
            return

    intent = _detect_intent(text)
    try:
        temporal = await get_temporal_client()
        handle = temporal.get_workflow_handle(bug.temporal_workflow_id)
        await handle.signal(BugInvestigationWorkflow.dev_reply, text, intent)
        ack = (
            ":robot_face: Got it! Creating a PR now..."
            if intent == "approve"
            else ":mag: Running a follow-up investigation with your additional context..."
        )
        await client.chat_postMessage(
            channel=bug.slack_channel_id, thread_ts=bug.slack_thread_ts, text=ack
        )
    except Exception:
        logger.exception("Failed to signal workflow for %s", bug.bug_id)
