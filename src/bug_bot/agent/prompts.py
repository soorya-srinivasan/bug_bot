def build_continuation_prompt(bug_id: str, conversation_ids: list[str], state: str) -> str:
    count = len(conversation_ids)
    if count:
        id_list = ", ".join(conversation_ids)
        recent = (
            f"{count} new message(s) arrived since your last turn "
            f"(conversation IDs: {id_list})."
        )
    else:
        recent = "No new messages since your last turn (continuing after timeout)."

    instruction = (
        f'Start by calling get_bug_conversations(bug_id="{bug_id}") to review the full '
        "conversation history for this bug. The most recent entries correspond to the "
        f"new message(s) above. Do not repeat a clarification question the reporter "
        "already answered — use the recorded answer in the history."
    )
    if state == "awaiting_reporter":
        instruction += (
            " The reporter's response is the most recent REPORTER entry in the history."
        )
    elif state == "awaiting_dev":
        instruction += (
            " If a developer message asks for a fix or PR, proceed with creating it. "
            "If the developer asks to close or resolve this bug, set action='resolved'."
        )
    return f"{recent}\n\n{instruction}"


def build_investigation_prompt(
    bug_id: str,
    description: str,
    severity: str,
    relevant_services: list[str],
    attachments: list[dict] | None = None,
) -> str:
    services_str = ", ".join(relevant_services) if relevant_services else "unknown"

    attachments_section = ""
    if attachments:
        lines = [
            f"- `{a['name']}` ({a.get('mimetype', 'unknown type')}) — available at `./attachments/{a['name']}`"
            for a in attachments
            if a.get("name")
        ]
        if lines:
            attachments_section = (
                "\n\n## Attachments\n"
                "The reporter attached the following files. They have been downloaded to your workspace:\n"
                + "\n".join(lines)
                + "\nUse the Read tool to inspect text/log files, or rely on your vision capability for images."
            )

    return f"""Investigate the following bug report and provide a structured analysis.

## Bug Report
- **Bug ID:** {bug_id}
- **Severity:** {severity}
- **Potentially Affected Services:** {services_str}

## Report Content
{description}{attachments_section}

## Investigation Steps
Follow these steps in order:

1. **Observability** — Query Grafana dashboards for recent anomalies (error spikes, latency increases, deployment markers) in the affected services. Query New Relic for recent errors, slow transactions, and exception traces.

2. **Code Search** — Search the GitHub organization for repositories related to the affected services. Look for recent commits, open issues, or PRs that might be related.

3. **Code Analysis** — Clone the most relevant repository. Examine the code paths mentioned or implied by the bug report. Look for obvious issues: null references, missing error handling, race conditions, N+1 queries.

4. **Data Check** — If the bug appears data-related, query PostgreSQL and/or MySQL databases (READ-ONLY) to check for data inconsistencies or unexpected values.

5. **Root Cause** — Synthesize your findings into a root cause assessment with a confidence level (0.0-1.0).

6. **Fix** — If the fix is straightforward and you have high confidence (>0.8), create a branch and submit a PR. If the fix is complex or you have low confidence, recommend escalation.

## Important
- All database access is READ-ONLY. Do not attempt writes.
- Call `list_services` early in your investigation to get all canonical service names.
  Use ONLY those exact names in the `relevant_services` field of your output — this
  ensures the right on-call engineers are paged on escalation.
- Use `lookup_service_owner` with the canonical name to get repo and team info.
- Use the `report_finding` tool to log significant observations during investigation.
- Be thorough but time-efficient. Focus on the most likely causes first."""
