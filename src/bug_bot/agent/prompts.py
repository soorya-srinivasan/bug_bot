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
        "already answered — use the recorded answer in the history. "
        "If you still need more information, set clarification_questions to a list of "
        "individual question strings (one question per list item)."
    )
    if state == "awaiting_reporter":
        instruction += (
            " The reporter's response is the most recent REPORTER entry in the history."
        )
    elif state == "awaiting_dev":
        instruction += (
            f" If the developer's message is NOT relevant to the current bug (e.g. general "
            "knowledge questions, off-topic chatter, math problems, or anything unrelated to "
            "the bug being investigated), do NOT answer it. Instead, set your summary to a "
            "polite message like: 'That question doesn't appear to be related to this bug "
            f"investigation. I can only assist with issues related to {bug_id}. Please ask "
            "your question in the relevant channel.' Set action='escalate' so the workflow "
            "keeps waiting for a relevant developer response. Do NOT run any tools or "
            "investigation for irrelevant messages."
            " If a developer message asks for a fix or PR, proceed with creating it. "
            "Set action='resolved' ONLY when the developer explicitly asks to close or resolve this bug "
            "(e.g. 'please close this', 'mark it as resolved', 'close the bug'). "
            "In ALL other cases — including when they say the bug is fixed, it's not a real issue, "
            "they'll handle it themselves, or any other statement — reply with a follow-up question "
            "asking whether the bug should be closed: post a clarification message and set action='escalate' "
            "so the workflow keeps waiting. "
            "Never infer closure intent. Require an explicit close instruction."
        )
    return f"{recent}\n\n{instruction}"


def _local_tz_label() -> str:
    """Return a human-readable label for the server's local timezone, e.g. 'IST (UTC+0530)'."""
    import datetime as _dt
    return _dt.datetime.now().astimezone().strftime("%Z (UTC%z)")


def build_investigation_prompt(
    bug_id: str,
    description: str,
    severity: str,
    relevant_services: list[str],
    attachments: list[dict] | None = None,
) -> str:
    services_str = ", ".join(relevant_services) if relevant_services else "unknown"
    local_tz = _local_tz_label()

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

> **GUARDRAIL**: All data must come from Grafana/Loki or GitHub MCP tools only.
> Do NOT use Read, Glob, Grep, or Bash to access system files during investigation.
> The local workspace is reserved exclusively for writing and committing code fixes.

0. **Pre-check — Validate report specificity (MANDATORY BEFORE ANY TOOL CALL)**
   Before calling any tool or querying any system, check if the following are present in
   the report. Ask only for what is missing:

   a. **Service name** — if the service is not clearly identified, ask:
      "Which service is affected?"
      Do NOT suggest or list any service names — ask openly and let the reporter provide it.

   b. **Domain-specific identifier** — ALWAYS required, even if the service is clear.
      Infer the most relevant identifier from the context of the report and ask for that
      specifically. Do NOT ask generically for trace IDs or correlation IDs. Examples:
        - "account details failing" → ask for the account ID
        - "payment not processed" → ask for the payment or order ID
        - "subscription error" → ask for the subscription ID
        - "user can't log in" → ask for the user ID or email
        - "invoice missing" → ask for the invoice ID
      If an appropriate identifier is already present in the report, do NOT ask for it again.

   Only ask for what is missing — if both are present, proceed directly to step 1.

   Set `action="clarify"`, populate `clarification_questions` with only the applicable
   questions, set `fix_type="needs_human"`, `confidence=0.0` and STOP.
   Do NOT call `list_services`, `lookup_service_owner`, or any log/DB/GitHub tool.
   Only continue to step 1 once both the service and a domain-specific identifier are available.

1. **Observability — Grafana Loki (MANDATORY FIRST STEP)** *(skill: logs_reading.md)*
   Grafana is running at http://localhost:3000 and Loki at http://localhost:3100.
   You MUST query Loki before doing anything else.

   **Timezone context:** The server's local timezone is **{local_tz}**.
   All timestamps returned by `query_loki_logs` are displayed in this timezone.
   When the reporter mentions a specific time (e.g. "around 2 PM"), treat it as
   **{local_tz}** unless they explicitly state otherwise.

   a. Call `mcp__bugbot_tools__list_datasources` to get the Loki datasource UID.
      - If this call fails or returns an error, set `summary` to:
        "Grafana Loki is unreachable (http://localhost:3000). Cannot fetch logs — manual investigation required."
        Set `fix_type="needs_human"`, `action="escalate"`, `confidence=0.0` and STOP. Do not proceed further.

   b. **User/entity identifier search (do this FIRST if an identifier is available):**
      If the bug report contains a user identifier (phone number, email, user ID, CR number,
      account name, order ID, etc.), query for ALL logs containing that identifier — not just
      errors. This shows the full request flow and reveals what actually happened:
        - `{{app="{services_str}"}} |= "<identifier>"`
        - `{{service="{services_str}"}} |= "<identifier>"`
        - `{{app=~".+"}} |= "<identifier>"` (search across all services if needed)
      This will show the request path, parameters received, decisions made, and any rejections
      or failures — even if they weren't logged as errors.

   c. **Error log search (do this in addition to or instead of identifier search):**
      Query for error logs using these patterns in order until you get results:
        - `{{app="{services_str}"}} |= "error"`
        - `{{app="{services_str}"}} |~ "(?i)error|exception|traceback"`
        - `{{service="{services_str}"}} |~ "(?i)error|exception|traceback"`
        - `{{service_name="{services_str}"}} |= "error"`
        - `{{job="{services_str}"}} |~ "(?i)error|exception|traceback"`
        - `{{}} |= "{services_str}" |~ "(?i)error|exception"` (broad fallback)
      Start with start_offset_minutes=60. If no logs are found, retry with start_offset_minutes=360.

   d. If the query tool call itself succeeds but returns no log lines, record that explicitly:
      "No logs found in Grafana Loki for service {services_str} in the last 6 hours."
      Continue to step 2 with this note in your summary.

   e. If logs ARE found:
      - Record the error type, message, frequency, and first/last occurrence using `mcp__bugbot_tools__report_finding`.
      - Note the exact log labels returned so you can use them in subsequent queries.
      - Construct a Grafana Explore deep-link using this exact format and encoding rules:

        URL structure:
        http://localhost:3000/explore?schemaVersion=1&panes=<encoded_panes>&orgId=1

        The panes JSON (before encoding) must follow this structure exactly:
        {{"3gq":{{"datasource":"<UID>","queries":[{{"refId":"A","expr":"<LogQL>","queryType":"range","datasource":{{"type":"loki","uid":"<UID>"}},"editorMode":"builder"}}],"range":{{"from":"<START_MS>","to":"<END_MS>"}}}}}}

        Where:
        - <LogQL> is the actual query string used.
        - <START_MS> and <END_MS> are the **millisecond timestamps printed by the tool**
          in the line "Grafana range → start_ms=<START_MS>  end_ms=<END_MS>".
          Copy those exact values — do NOT compute them yourself from the current date,
          as doing so produces incorrect anchors.

        Encoding rules for the panes value — percent-encode ONLY these characters:
          "  → %22       {{  → %7B       }}  → %7D
          [  → %5B       ]  → %5D       space → %20
          |  → %7C       =  → %3D       \  → %5C
          `  → %60
        Do NOT encode : or , — leave them as literal characters in the URL.

        Example panes JSON (before encoding) with start_ms=1771806085000 end_ms=1771813285000
        and query `{{app="payment-service-sample"}} |= "abc123"` with UID `P8E80F9AEF21F6940`:
        {{"3gq":{{"datasource":"P8E80F9AEF21F6940","queries":[{{"refId":"A","expr":"{{app=\"payment-service-sample\"}} |= \"abc123\"","queryType":"range","datasource":{{"type":"loki","uid":"P8E80F9AEF21F6940"}},"editorMode":"builder"}}],"range":{{"from":"1771806085000","to":"1771813285000"}}}}}}

        Set this URL in the `grafana_logs_url` field of your response.

2. **Code Search** *(skill: service_team_fetching.md)* — Call `mcp__bugbot_tools__list_services`
   early to get all canonical service names. If the bug mentions any term that matches a service's
   name or description, include that service in your investigation even if it wasn't explicitly named.
   When multiple services could be involved, investigate all of them.
   Use `mcp__bugbot_tools__lookup_service_owner` to find the GitHub repo for each affected service.
   Then use `mcp__github__search_code` or `mcp__github__get_file_contents` to locate the function
   or file mentioned in the error traceback from the logs.
   Do NOT clone the repo — all code reading must go through GitHub MCP tools.

3. **Code Analysis** *(skill: dotnet_debugging.md or rails_debugging.md)* — Use
   `mcp__github__get_file_contents` to read the specific file and function from the traceback.
   Look for: unguarded division, missing null/zero checks, missing error handling.
   Then identify the commit that introduced the bug:
   - Use `mcp__github__list_commits` with the file path to find recent commits touching the file.
   - Use `mcp__github__get_commit` on the suspect commit hash to confirm the change and get the
     full author name, email, and date.
   - Record: commit hash (short), author name, author email, commit date, and commit message.
   Include this in the PR body as: `Introduced by: <author> (<email>) in <hash> on <date>: "<message>"`

4. **Data Check** *(skill: database_investigation.md)* — If the bug appears data-related (not a
   code bug), use `mcp__postgres__*` or `mcp__mysql__*` to run READ-ONLY queries (SELECT only,
   always LIMIT results). Scope ALL queries to the specific identifier provided (trace ID,
   request ID, order ID, user ID, etc.) — do NOT run broad queries that return unrelated records.

5. **Root Cause** *(skill: plan.md)* — Synthesize findings into a root cause assessment with a confidence level (0.0–1.0).

6. **Action — choose based on root cause:** *(skill: pr_execution.md if code fix)*

   **IF root cause is a CODE BUG and confidence > 0.8:**

   For EACH affected service (you MUST fix ALL services where you found issues):

   **Step A — Prepare the fix:**
   - Read the file: `mcp__github__get_file_contents(owner, repo, path, ref='main')`
   - Create branch: `mcp__github__create_branch(owner, repo, branch='bugfix/<bug_id>-<short-desc>', from_branch='main')`
   - Prepare the corrected file content in-context

   **Step B — Code review (MANDATORY — DO NOT SKIP):**
   You MUST use the Task tool to spawn the `code-reviewer` subagent and pass it the diff.
   Example:
   ```
   Task(subagent_type="code-reviewer", prompt="Review this fix for <repo>:
   File: <path>
   Original:
   ```
   <original code snippet>
   ```
   Proposed:
   ```
   <your fixed code snippet>
   ```
   Root cause: <what the bug was>")
   ```
   - If reviewer says **APPROVED** → proceed to Step C
   - If reviewer says **CHANGES REQUESTED** → apply changes, re-submit to code-reviewer
   - **NEVER call mcp__github__push_files or mcp__github__create_or_update_file without APPROVED review**

   **Step C — Commit and push (only after APPROVED review):**
   - `mcp__github__push_files(owner, repo, branch, files=[{{path, content}}], message)`
     OR `mcp__github__create_or_update_file(owner, repo, path, message, content, branch, sha)`

   **Step D — Create the PR:**
   - `mcp__github__create_pull_request(owner, repo, title='fix: <desc> [{bug_id}]', body, head, base='main')`
   - PR body must include a "Culprit Commit" section:
     ```
     ## Culprit Commit
     Introduced by **<author name>** (<author email>)
     Commit: `<short hash>` — <commit message>
     Date: <commit date>
     ```

   **Repeat Steps A–D for every affected service/repo.** Do not stop after the first fix.

   If you cannot fix a particular service (unfamiliar stack, too complex), you MUST:
     - Describe the exact issue, affected file path, and line number in `summary`
     - Add a specific recommended action like "Fix <file> in <repo>: <what needs to change>"

   After all fixes:
   - Set `pr_urls` to a list of `{{"repo": "<repo>", "branch": "<branch>", "pr_url": "<url>", "service": "<service>"}}`
   - Set `pr_url` to the first/primary PR URL (backward compat)
   - Set `fix_type="code_fix"`
   - Set `culprit_commit` to `{{"hash": "<short_hash>", "author": "<name>", "email": "<email>", "date": "<date>", "message": "<commit message>"}}`
   - In `summary`, include ALL issues found across all services, even if you only fixed some.
     Format: `"Root cause: <cause>. PRs created: <pr_urls>. Additional issue: <description>"`

   **ELSE IF root cause is a DATA ISSUE (bad/missing/corrupt data in DB):**
   - Use `mcp__postgres__*` or `mcp__mysql__*` to run the specific READ-ONLY query
     that demonstrates the problem
   - Include both the query and its result rows in `summary`, formatted as:
     ```
     <query>
     ```
     ```
     <result rows>
     ```
   - Set `fix_type="data_fix"`, `pr_url=null`
   - Do NOT create a PR

   **ELSE (logs found but root cause unclear, or confidence ≤ 0.8):**
   - Summarize what the logs showed: error type, frequency, first/last occurrence
   - Include a representative log line sample in `summary` wrapped in a code block:
     ```
     <log line(s)>
     ```
   - Set `fix_type="unknown"` or `fix_type="needs_human"`, `pr_url=null`
   - Do NOT create a PR

## Important
- Whenever you include a query (SQL, LogQL, or similar) or raw data/result rows in any
  output field (`summary`, `root_cause`, etc.), always wrap it in a triple-backtick code
  block (```) so it renders correctly in Slack.
- All database access is READ-ONLY. Do not attempt writes.
- All code access must go through GitHub MCP (`mcp__github__*`). Do not clone repos or read
  local files for investigation — use `mcp__github__get_file_contents` and `mcp__github__search_code`.
- All log access must go through Grafana/Loki (`mcp__bugbot_tools__query_loki_logs`). Do not
  read log files from the local filesystem.
- Call `list_services` early in your investigation to get all canonical service names.
  Use ONLY those exact names in the `relevant_services` field of your output — this
  ensures the right on-call engineers are paged on escalation.
- Use `lookup_service_owner` with the canonical name to get repo and team info.
- Use the `report_finding` tool to log significant observations during investigation.
- Populate `skills_used` in your output with every skill file you consulted during this
  investigation (e.g. `["plan.md", "logs_reading.md", "service_team_fetching.md"]`).
- Be thorough but time-efficient. Focus on the most likely causes first."""
