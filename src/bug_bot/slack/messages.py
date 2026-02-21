def format_investigation_result(result: dict, bug_id: str) -> list[dict]:
    """Format investigation results as Slack Block Kit blocks."""
    confidence = result.get("confidence", 0)
    if confidence > 0.8:
        confidence_emoji = ":large_green_circle:"
    elif confidence > 0.5:
        confidence_emoji = ":large_yellow_circle:"
    else:
        confidence_emoji = ":red_circle:"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Investigation Results - {bug_id}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Fix Type:* `{result['fix_type']}`"},
                {"type": "mrkdwn", "text": f"*Confidence:* {confidence_emoji} {confidence:.0%}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Summary:*\n{result['summary']}"},
        },
    ]

    if result.get("root_cause"):
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Root Cause:*\n{result['root_cause']}"},
            }
        )

    if result.get("grafana_logs_url"):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":mag: *Logs:* <{result['grafana_logs_url']}|View in Grafana Loki>",
                },
            }
        )

    if result.get("culprit_commit"):
        cc = result["culprit_commit"]
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":git: *Culprit Commit:* `{cc.get('hash', 'unknown')}`\n"
                        f"*Author:* {cc.get('author', 'unknown')} ({cc.get('email', '')})\n"
                        f"*Date:* {cc.get('date', 'unknown')}\n"
                        f"*Message:* _{cc.get('message', '')}_"
                    ),
                },
            }
        )

    if result.get("pr_url"):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":pr: *Pull Request:* <{result['pr_url']}|View PR>",
                },
            }
        )

    if result.get("recommended_actions"):
        actions_text = "\n".join(f"  - {a}" for a in result["recommended_actions"])
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Recommended Actions:*\n{actions_text}"},
            }
        )

    return blocks


def format_summary_message(
    bug_id: str,
    severity: str,
    result: dict,
    original_channel: str,
    original_thread_ts: str,
) -> list[dict]:
    """Format summary message for #bug-summaries channel."""
    status = "Resolved" if result.get("pr_url") else "Escalated"
    thread_link = (
        f"https://slack.com/archives/{original_channel}/p{original_thread_ts.replace('.', '')}"
    )

    text = (
        f"*{bug_id}* | Severity: `{severity}` | Status: *{status}*\n"
        f"{result['summary']}\n"
        f"<{thread_link}|View original thread>"
    )

    if result.get("grafana_logs_url"):
        text += f"\n:mag: <{result['grafana_logs_url']}|View logs in Grafana Loki>"

    if result.get("pr_url"):
        text += f"\n:pr: <{result['pr_url']}|View PR>"

    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]


def format_triage_response(triage: dict, bug_id: str) -> str:
    """Format the initial triage acknowledgement as mrkdwn text."""
    severity = triage.get("severity", "P3")
    category = triage.get("category", "unknown")
    summary = triage.get("summary", "")
    services = triage.get("affected_services", [])
    services_str = ", ".join(f"`{s}`" for s in services) if services else "_unknown_"

    return (
        f":mag: *Bug Bot* received this report (`{bug_id}`).\n"
        f"*Severity:* `{severity}`\n"
        f"*Summary:* {summary}\n"
        f"I'm starting an investigation and will update this thread."
    )


def format_investigation_as_markdown(result: dict, bug_id: str) -> str:
    """Render investigation details as a Markdown document (for file-upload mode)."""
    confidence = result.get("confidence", 0)
    lines = [
        f"# Investigation Results — {bug_id}",
        "",
        f"**Fix Type:** `{result.get('fix_type', 'unknown')}`  ",
        f"**Confidence:** {confidence:.0%}",
        "",
        "## Summary",
        "",
        result.get("summary", ""),
        "",
    ]
    if result.get("root_cause"):
        lines += ["## Root Cause", "", result["root_cause"], ""]
    if result.get("grafana_logs_url"):
        lines += ["## Logs", "", f"[View in Grafana Loki]({result['grafana_logs_url']})", ""]
    if result.get("culprit_commit"):
        cc = result["culprit_commit"]
        lines += [
            "## Culprit Commit",
            "",
            f"**Author:** {cc.get('author', 'unknown')} ({cc.get('email', '')})",
            f"**Commit:** `{cc.get('hash', 'unknown')}` — {cc.get('message', '')}",
            f"**Date:** {cc.get('date', 'unknown')}",
            "",
        ]
    if result.get("pr_url"):
        lines += ["## Pull Request", "", f"[View PR]({result['pr_url']})", ""]
    if result.get("recommended_actions"):
        lines += ["## Recommended Actions", ""]
        for action in result["recommended_actions"]:
            lines.append(f"- {action}")
        lines.append("")
    return "\n".join(lines)


def format_followup_question(question: str) -> list[dict]:
    """Format a follow-up question from the agent as Slack blocks."""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f":speech_balloon: *Bug Bot has a question:*\n{question}",
            },
        },
    ]
