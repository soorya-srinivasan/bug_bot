import logging
import re
from datetime import datetime, timedelta

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from bug_bot.config import settings
from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository
from bug_bot.duplicate import check_duplicate_bug
from bug_bot.slack.messages import format_triage_response
from bug_bot.temporal.client import get_temporal_client
from bug_bot.temporal import BugReportInput
from bug_bot.temporal.workflows.bug_investigation import BugInvestigationWorkflow
from bug_bot.triage import triage_bug_report
from bug_bot.redact import redact_for_reporters

logger = logging.getLogger(__name__)

_CLOSE_RE = re.compile(
    r'\b(close|cancel|nevermind|never mind|withdraw|ignore this|not an issue|'
    r'already fixed|already resolved|issue is resolved|resolved now|no longer needed)\b',
    re.IGNORECASE,
)


def _extract_text_from_blocks(blocks: list) -> str:
    """Extract plain text from Slack Block Kit blocks.

    Slack sometimes sends rich-text blocks (links, phone numbers formatted as
    tel: anchors, code spans, etc.) without including them in the `text` field.
    This walks the block tree and reconstructs the human-readable content so
    phone numbers, emails, and code snippets are never lost.
    """
    parts = []
    for block in blocks:
        btype = block.get("type", "")
        if btype in ("rich_text", "section"):
            for element in block.get("elements", []):
                parts.extend(_extract_from_element(element))
            # section blocks may have a text object instead of elements
            if "text" in block and isinstance(block["text"], dict):
                parts.append(block["text"].get("text", ""))
        elif btype == "header":
            if isinstance(block.get("text"), dict):
                parts.append(block["text"].get("text", ""))
        elif btype == "context":
            for el in block.get("elements", []):
                parts.extend(_extract_from_element(el))
    return " ".join(p for p in parts if p).strip()


def _extract_from_element(element: dict) -> list[str]:
    """Recursively extract text from a single block element."""
    etype = element.get("type", "")
    parts = []
    if etype in ("rich_text_section", "rich_text_preformatted", "rich_text_quote"):
        for child in element.get("elements", []):
            parts.extend(_extract_from_element(child))
    elif etype == "rich_text_list":
        for child in element.get("elements", []):
            parts.extend(_extract_from_element(child))
    elif etype == "text":
        parts.append(element.get("text", ""))
    elif etype == "link":
        # Use display text if available, else the URL (strips tel:/ mailto: prefix)
        display = element.get("text") or element.get("url", "")
        if display.startswith("tel:"):
            display = display[4:]
        elif display.startswith("mailto:"):
            display = display[7:]
        parts.append(display)
    elif etype == "user":
        parts.append(f"@{element.get('user_id', '')}")
    elif etype == "channel":
        parts.append(f"#{element.get('channel_id', '')}")
    elif etype in ("mrkdwn", "plain_text"):
        parts.append(element.get("text", ""))
    return parts


def register_handlers(app: AsyncApp):

    @app.event("message")
    async def handle_message(event: dict, client: AsyncWebClient):
        channel_id = event.get("channel")
        subtype = event.get("subtype")
        # Only process plain user messages and file shares; ignore everything else.
        _ALLOWED_SUBTYPES = {None, "file_share"}
        if event.get("bot_id") or subtype not in _ALLOWED_SUBTYPES:
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

        # ── @mention gate (feature flag) ─────────────────────────────────────────
        if settings.require_bot_mention and settings.slack_bot_user_id:
            mention_token = f"<@{settings.slack_bot_user_id}>"
            raw_text = event.get("text", "")
            # Also scan blocks in case the mention lives there
            block_text_for_scan = ""
            if event.get("blocks"):
                block_text_for_scan = _extract_text_from_blocks(event["blocks"])
            if mention_token not in raw_text and f"@{settings.slack_bot_user_id}" not in block_text_for_scan:
                return  # Not mentioned — ignore silently

        thread_ts = event["ts"]
        reporter = event.get("user", "unknown")
        text = event.get("text", "")
        # Augment with block content — Slack rich-text blocks can contain phone
        # numbers as tel: links and other structured data absent from plain `text`.
        if event.get("blocks"):
            block_text = _extract_text_from_blocks(event["blocks"])
            if block_text and block_text not in text:
                text = f"{text}\n{block_text}".strip() if text else block_text

        # Strip bot mention from the text so it doesn't appear in the bug report
        if settings.require_bot_mention and settings.slack_bot_user_id:
            text = re.sub(
                r'<@' + re.escape(settings.slack_bot_user_id) + r'(?:\|[^>]+)?>',
                '',
                text,
            ).strip()

        bug_id = f"BUG-{int(float(thread_ts))}"
        workflow_id = f"bug-{thread_ts.replace('.', '-')}"

        # Extract file attachments (images, logs, etc.)
        attachments = [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "mimetype": f.get("mimetype"),
                "filetype": f.get("filetype"),
                "size": f.get("size"),
                "url_private": f.get("url_private"),
                "permalink": f.get("permalink"),
            }
            for f in event.get("files", [])
            if f.get("url_private")
        ]

        # Run triage classification
        triage = await triage_bug_report(text, reporter)

        # ── Duplicate detection (feature flag) ────────────────────────────────
        if settings.enable_duplicate_detection:
            dup_since = datetime.utcnow() - timedelta(hours=settings.duplicate_check_window_hours)
            async with async_session() as _s:
                recent_bugs = await BugRepository(_s).get_recent_open_bugs(since=dup_since)
            candidates = [
                {"bug_id": b.bug_id, "message": b.original_message}
                for b in recent_bugs
            ]
            dup = await check_duplicate_bug(text, triage.get("summary", ""), candidates)
            if dup and dup["confidence"] >= settings.duplicate_similarity_threshold:
                dup_bug_id = dup["bug_id"]
                async with async_session() as _s:
                    orig = await BugRepository(_s).get_bug_by_id(dup_bug_id)
                thread_link = (
                    f"slack://channel?team=&id={orig.slack_channel_id}&thread_ts={orig.slack_thread_ts}"
                    if orig else ""
                )
                await client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=(
                        f":warning: This report looks similar to an existing open bug: *{dup_bug_id}*.\n"
                        f"Please add your information to that thread"
                        + (f": <{thread_link}|view thread>" if thread_link else ".")
                        + "\nIf your issue is genuinely different, re-post with more specific details."
                    ),
                )
                return  # Do NOT insert to DB or start workflow
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
                attachments=attachments,
            )
            await repo.log_conversation(
                bug_id=bug_id,
                message_type="bug_report",
                sender_type="reporter",
                sender_id=reporter,
                channel=channel_id,
                message_text=text,
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
                attachments=attachments,
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
    """Handle a human reply in #bug-reports.

    Only processes messages from the original reporter — ignores everyone else
    so that teammates tagging the reporter don't feed noise into the
    clarification wait.
    """
    channel_id = event["channel"]
    thread_ts = event["thread_ts"]
    text = event.get("text", "")
    print("EVENT THREADDDD")
    print(event)
    if event.get("blocks"):
        block_text = _extract_text_from_blocks(event["blocks"])
        if block_text and block_text not in text:
            text = f"{text}\n{block_text}".strip() if text else block_text

    if not text or text.startswith("!"):
        return

    async with async_session() as session:
        repo = BugRepository(session)
        bug = await repo.get_bug_by_thread_ts(channel_id, thread_ts)
        if not bug:
            return

    # ── Only process messages from the original reporter ─────────────────────
    # If a teammate tags the reporter or asks for an update, their message is
    # NOT a clarification answer and must not wake the wait_condition.
    if event.get("user") != bug.reporter_user_id:
        return

    if not bug.temporal_workflow_id or bug.status in ("resolved", "escalated"):
        return

    # ── Rate limiting ─────────────────────────────────────────────────────────
    rate_window_start = datetime.utcnow() - timedelta(seconds=settings.reporter_reply_rate_window_secs)
    async with async_session() as _s:
        recent_count = await BugRepository(_s).count_recent_reporter_replies(
            bug.bug_id, since=rate_window_start
        )
    if recent_count >= settings.reporter_reply_rate_limit:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=(
                ":hourglass: We've received your previous messages and the investigation "
                "is catching up. Please wait a moment before sending more updates."
            ),
        )
        return

    temporal = await get_temporal_client()
    handle = temporal.get_workflow_handle(bug.temporal_workflow_id)

    try:
        # ── Reporter wants to close the bug ──────────────────────────────────
        if _CLOSE_RE.search(text):
            await handle.signal(BugInvestigationWorkflow.close_requested)
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=(
                    f":white_check_mark: Got it — `{bug.bug_id}` will be closed. "
                    "Thanks for the update!"
                ),
            )
        else:
            # ── Reporter message (clarification answer or additional context) ─
            # Persist to DB first so the agent can read it via get_bug_conversations
            # even mid-investigation. Signal carries only the conversation UUID.
            reply_attachments = [
                {
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "mimetype": f.get("mimetype"),
                    "filetype": f.get("filetype"),
                    "size": f.get("size"),
                    "url_private": f.get("url_private"),
                    "permalink": f.get("permalink"),
                }
                for f in event.get("files", [])
                if f.get("url_private")
            ]
            async with async_session() as session:
                repo_log = BugRepository(session)
                convo = await repo_log.log_conversation(
                    bug_id=bug.bug_id,
                    message_type="reporter_reply",
                    sender_type="reporter",
                    sender_id=event.get("user"),
                    channel=channel_id,
                    message_text=text,
                    metadata={"attachments": reply_attachments} if reply_attachments else None,
                )
                convo_id = str(convo.id)
            await handle.signal(
                BugInvestigationWorkflow.incoming_message,
                args=["reporter", event.get("user", "unknown"), convo_id],
            )
            await client.chat_postMessage(
                channel=channel_id,
                thread_ts=thread_ts,
                text=":memo: Your response has been logged and the investigation will resume shortly.",
            )
    except Exception:
        logger.exception("Failed to signal workflow %s for %s", bug.temporal_workflow_id, bug.bug_id)


async def _handle_summary_thread_reply(event: dict, client: AsyncWebClient):
    """Handle a reply in #bug-summaries — signal the workflow with dev decision."""
    thread_ts = event["thread_ts"]
    text = event.get("text", "")

    # Allow file-only messages (no text body) from devs
    if not text and not event.get("files"):
        return

    async with async_session() as session:
        repo = BugRepository(session)
        bug = await repo.get_bug_by_summary_thread_ts(thread_ts)
        if not bug or not bug.temporal_workflow_id:
            return

    try:
        temporal = await get_temporal_client()
        handle = temporal.get_workflow_handle(bug.temporal_workflow_id)

        # ── Dev wants to close the bug ────────────────────────────────────────
        if text and _CLOSE_RE.search(text):
            await handle.signal(BugInvestigationWorkflow.close_requested)
            # Ack in #bug-summaries — include the dev's reason so there's context.
            ack_text = f":white_check_mark: Got it — `{bug.bug_id}` will be closed.\n*Reason:* {text}"
            await client.chat_postMessage(
                channel=event["channel"],
                thread_ts=thread_ts,
                text=ack_text,
            )
            # Notify the reporter in the original #bug-reports thread.
            # Redact PII / sensitive org info from the reason before it reaches
            # the reporter-facing channel; the summary channel keeps the original.
            async with async_session() as _s:
                inv = await BugRepository(_s).get_investigation(bug.bug_id)
            pr_url = inv.pr_url if inv else None
            safe_reason = await redact_for_reporters(text)
            closure_text = (
                f":white_check_mark: *{bug.bug_id}* has been marked as resolved.\n"
                f"*Reason:* {safe_reason}"
            )
            if pr_url:
                closure_text += f"\nA fix has been submitted: <{pr_url}|View PR>"
            await client.chat_postMessage(
                channel=bug.slack_channel_id,
                thread_ts=bug.slack_thread_ts,
                text=closure_text,
            )
            return

        # ── Normal dev message: persist to DB first, signal with UUID ─────────
        dev_attachments = [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "mimetype": f.get("mimetype"),
                "filetype": f.get("filetype"),
                "size": f.get("size"),
                "url_private": f.get("url_private"),
                "permalink": f.get("permalink"),
            }
            for f in event.get("files", [])
            if f.get("url_private")
        ]
        async with async_session() as session:
            repo = BugRepository(session)
            convo = await repo.log_conversation(
                bug_id=bug.bug_id,
                message_type="dev_reply",
                sender_type="developer",
                sender_id=event.get("user"),
                channel=event["channel"],
                message_text=text,
                metadata={"attachments": dev_attachments} if dev_attachments else None,
            )
            convo_id = str(convo.id)
        await handle.signal(
            BugInvestigationWorkflow.incoming_message,
            args=["developer", event.get("user", "unknown"), convo_id],
        )
        await client.chat_postMessage(
            channel=event["channel"],
            thread_ts=thread_ts,
            text=":mag: Your message has been forwarded to the investigation.",
        )
    except Exception:
        logger.exception("Failed to signal workflow for %s", bug.bug_id)
