from dataclasses import dataclass, field

from temporalio import activity

from bug_bot.config import settings
from bug_bot.slack.messages import format_investigation_result, format_summary_message
from bug_bot.temporal import InvestigationResult


def _slack_configured() -> bool:
    return bool(settings.slack_bot_token) and not settings.slack_bot_token.startswith("xoxb-your")


def _get_slack_client():
    from slack_sdk.web.async_client import AsyncWebClient
    return AsyncWebClient(token=settings.slack_bot_token)


@dataclass
class PostMessageInput:
    channel_id: str
    thread_ts: str
    text: str


@dataclass
class PostResultsInput:
    channel_id: str
    thread_ts: str
    bug_id: str
    severity: str
    result: dict  # serialized InvestigationResult


@dataclass
class EscalationInput:
    channel_id: str
    thread_ts: str
    bug_id: str
    severity: str
    relevant_services: list[str]
    escalation_level: int = 1
    oncall_entries: list[dict] = field(default_factory=list)  # [{oncall_engineer, slack_group_id}, ...]


@activity.defn
async def post_slack_message(input: PostMessageInput) -> None:
    """Post a simple text message in a Slack thread."""
    if not _slack_configured():
        activity.logger.info(f"[Slack skip] #{input.channel_id}: {input.text}")
        return
    client = _get_slack_client()
    await client.chat_postMessage(
        channel=input.channel_id,
        thread_ts=input.thread_ts,
        text=input.text,
    )


@activity.defn
async def post_investigation_results(input: PostResultsInput) -> None:
    """Post a brief acknowledgment in the original bug thread; full results go to #bug-summaries."""
    fix_type = input.result.get("fix_type", "unknown")
    summary_channel = settings.bug_summaries_channel_id or "#bug-summaries"
    text = (
        f":white_check_mark: Investigation complete for `{input.bug_id}`. "
        f"Fix type: `{fix_type}`. "
        f"Full details posted in <#{summary_channel}>."
    )
    if not _slack_configured():
        activity.logger.info(f"[Slack skip] {text}")
        return
    client = _get_slack_client()
    await client.chat_postMessage(
        channel=input.channel_id,
        thread_ts=input.thread_ts,
        text=text,
    )


@activity.defn
async def create_summary_thread(input: PostResultsInput) -> str:
    """Post full investigation results to #bug-summaries. Returns the message ts."""
    if not _slack_configured():
        activity.logger.info(
            f"[Slack skip] Summary for {input.bug_id} (severity={input.severity}): "
            f"{input.result.get('summary')}"
        )
        return ""
    client = _get_slack_client()
    # Header block linking back to original thread
    header_blocks = format_summary_message(
        bug_id=input.bug_id,
        severity=input.severity,
        result=input.result,
        original_channel=input.channel_id,
        original_thread_ts=input.thread_ts,
    )
    # Full investigation detail blocks
    detail_blocks = format_investigation_result(input.result, input.bug_id)
    blocks = header_blocks + [{"type": "divider"}] + detail_blocks
    response = await client.chat_postMessage(
        channel=settings.bug_summaries_channel_id,
        blocks=blocks,
        text=f"Bug investigation results: {input.bug_id}",
    )
    return response.get("ts", "")


@activity.defn
async def escalate_to_humans(input: EscalationInput) -> None:
    """Tag relevant devs/L1 in the Slack thread."""
    msg = (
        f"Escalation (Level {input.escalation_level}) for {input.bug_id} "
        f"(Severity: {input.severity}) — "
        f"Services: {', '.join(input.relevant_services) or 'Unknown'} — "
        f"Requires human investigation."
    )

    parts = []
    for entry in input.oncall_entries:
        if entry.get("slack_group_id"):
            parts.append(f"<!subteam^{entry['slack_group_id']}>")
        elif entry.get("oncall_engineer"):
            parts.append(f"<@{entry['oncall_engineer']}>")
    mention_str = " ".join(parts)

    text = f":rotating_light: *{msg}*"
    if mention_str:
        text += f"\n{mention_str} please investigate."

    if not _slack_configured():
        activity.logger.info(f"[Slack skip] {text}")
        return
    client = _get_slack_client()
    await client.chat_postMessage(
        channel=input.channel_id,
        thread_ts=input.thread_ts,
        text=text,
    )


@activity.defn
async def send_follow_up(input: PostMessageInput) -> None:
    """Send a periodic follow-up reminder in the thread."""
    if not _slack_configured():
        activity.logger.info(f"[Slack skip] Follow-up #{input.channel_id}: {input.text}")
        return
    client = _get_slack_client()
    await client.chat_postMessage(
        channel=input.channel_id,
        thread_ts=input.thread_ts,
        text=input.text,
    )
