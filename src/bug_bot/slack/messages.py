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

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{bug_id}* | Severity: `{severity}` | Status: *{status}*\n"
                    f"{result['summary']}\n"
                    f"<{thread_link}|View original thread>"
                ),
            },
        },
    ]

    if result.get("pr_url"):
        blocks[0]["text"]["text"] += f"\n<{result['pr_url']}|View PR>"

    return blocks
