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

1. **Observability — Grafana Loki**
   Use the Grafana MCP (`mcp__grafana__*`) to query Loki for error logs from the affected services.
   Steps:
   a. Call `mcp__grafana__list_datasources` to find the Loki datasource UID.
   b. Call `mcp__grafana__query_loki_logs` with the UID and a LogQL query.
      LogQL patterns (substitute the actual service name):
        `{{app="{services_str}", level="error"}} |~ "ZeroDivisionError|Exception|Error|Traceback"`
        `{{app="{services_str}", env="local"}} | json | level="error"`
      Log labels used by services: `app`, `env`, `level`.
   If a tool name is uncertain, first list all `mcp__grafana__*` tools and use the appropriate one.
   Use `mcp__bugbot_tools__report_finding` to record significant errors found in the logs.
   c. After querying, construct a direct Grafana Explore link using the GRAFANA_URL env var,
      the datasource UID, and the exact LogQL query you used. Format:
      `<GRAFANA_URL>/explore?orgId=1&left=%7B"datasource":"<UID>","queries":%5B%7B"expr":"<URL-encoded LogQL>","refId":"A"%7D%5D,"range":%7B"from":"now-1h","to":"now"%7D%7D`
      Set this URL in the `grafana_logs_url` field of your response.

2. **Code Search** — Use `mcp__bugbot_tools__lookup_service_owner` to find the GitHub repo for
   each affected service. Then use `mcp__github__search_code` or `mcp__github__get_file_contents`
   to locate the function or file mentioned in the error traceback from the logs.

3. **Code Analysis** — Use `mcp__git__clone_repository` to clone the repo to
   `/tmp/bugbot-workspace/<repo-name>`. Read the specific file and function from the traceback.
   Look for: unguarded division, missing null/zero checks, missing error handling.
   Then identify the commit that introduced the bug:
   - Run `git blame -L <start>,<end> <file>` on the affected lines to find the commit hash and author.
   - Run `git show <hash> --stat` to confirm the change and get the full author name, email, and date.
   - Record: commit hash (short), author name, author email, commit date, and commit message.
   Include this in the PR body as: `Introduced by: <author> (<email>) in <hash> on <date>: "<message>"`

4. **Data Check** — If the bug appears data-related (not a code bug), use `mcp__postgres__*` or
   `mcp__mysql__*` to run READ-ONLY queries (SELECT only, always LIMIT results) to check for
   data inconsistencies or unexpected values.

5. **Root Cause** — Synthesize findings into a root cause assessment with a confidence level (0.0–1.0).

6. **Action — choose based on root cause:**

   **IF root cause is a CODE BUG and confidence > 0.8:**
   - Use `mcp__git__create_branch` to create a branch: `<bug_id>-<short-desc>`
   - Make the minimal fix (add a guard clause only — do not refactor surrounding code)
   - Use `mcp__git__commit` with message: `fix(<service>): <description> [<bug_id>]`
   - Use `mcp__git__push` to push the branch
   - Use `mcp__github__create_pull_request` to open a PR
     - PR title must include the bug ID: `fix: <description> [<bug_id>]`
     - PR body must include a "Culprit Commit" section:
       ```
       ## Culprit Commit
       Introduced by **<author name>** (<author email>)
       Commit: `<short hash>` — <commit message>
       Date: <commit date>
       ```
   - Set `fix_type="code_fix"`, `pr_url=<pr_url>`
   - Set `culprit_commit` to `{{"hash": "<short_hash>", "author": "<name>", "email": "<email>", "date": "<date>", "message": "<commit message>"}}`
   - In `summary`, include: `"Root cause: <cause>. PR created: <pr_url>"`

   **ELSE IF root cause is a DATA ISSUE (bad/missing/corrupt data in DB):**
   - Use `mcp__postgres__*` or `mcp__mysql__*` to run the specific READ-ONLY query
     that demonstrates the problem
   - Include both the query and its result rows in `summary`
   - Set `fix_type="data_fix"`, `pr_url=null`
   - Do NOT create a PR

   **ELSE (logs found but root cause unclear, or confidence ≤ 0.8):**
   - Summarize what the logs showed: error type, frequency, first/last occurrence
   - Include a representative log line sample in `summary`
   - Set `fix_type="unknown"` or `fix_type="needs_human"`, `pr_url=null`
   - Do NOT create a PR

## Important
- All database access is READ-ONLY. Do not attempt writes.
- Call `list_services` early in your investigation to get all canonical service names.
  Use ONLY those exact names in the `relevant_services` field of your output — this
  ensures the right on-call engineers are paged on escalation.
- Use `lookup_service_owner` with the canonical name to get repo and team info.
- Use the `report_finding` tool to log significant observations during investigation.
- Be thorough but time-efficient. Focus on the most likely causes first."""
