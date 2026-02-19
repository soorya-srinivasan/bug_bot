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


_STATUS_REPLIES = {
    "new": ":hourglass: Your report is queued for triage, hang tight!",
    "triaged": ":mag: Investigation is starting soon...",
    "investigating": ":robot_face: I'm currently investigating this bug. Check <#{channel}> for updates once done.",
    "awaiting_dev": ":speech_balloon: Waiting for a developer decision in <#{channel}>.",
    "escalated": ":rotating_light: This bug has been escalated. A developer is looking at it.",
    "resolved": ":white_check_mark: This bug has already been resolved.",
}


async def _handle_bug_thread_reply(event: dict, client: AsyncWebClient):
    """Handle a human reply in #bug-reports — respond with current bug status."""
    channel_id = event["channel"]
    thread_ts = event["thread_ts"]
    text = event.get("text", "")

    # Ignore bot messages and commands handled elsewhere
    if not text or text.startswith("!"):
        return

    async with async_session() as session:
        repo = BugRepository(session)
        bug = await repo.get_bug_by_thread_ts(channel_id, thread_ts)
        if not bug:
            return

    summary_channel = settings.bug_summaries_channel_id or "bug-summaries"
    reply = _STATUS_REPLIES.get(
        bug.status,
        f":information_source: Bug `{bug.bug_id}` is in status `{bug.status}`.",
    ).replace("{channel}", summary_channel)

    await client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=reply)


async def _handle_summary_thread_reply(event: dict, client: AsyncWebClient):
    """Handle a reply in #bug-summaries — signal the workflow with dev decision."""
    thread_ts = event["thread_ts"]
    text = event.get("text", "")

    if not text:
        return

    async with async_session() as session:
        repo = BugRepository(session)
        bug = await repo.get_bug_by_summary_thread_ts(thread_ts)
        if not bug or not bug.temporal_workflow_id:
            return

    intent = _detect_intent(text)
    try:
        temporal = await get_temporal_client()
        handle = temporal.get_workflow_handle(bug.temporal_workflow_id)
        await handle.signal(BugInvestigationWorkflow.dev_reply, args=[text, intent])
        ack = (
            ":robot_face: Got it! Creating a PR now..."
            if intent == "approve"
            else ":mag: Running a follow-up investigation with your additional context..."
        )
        # Ack in #bug-summaries thread
        await client.chat_postMessage(
            channel=event["channel"], thread_ts=thread_ts, text=ack
        )
    except Exception:
        logger.exception("Failed to signal workflow for %s", bug.bug_id)
