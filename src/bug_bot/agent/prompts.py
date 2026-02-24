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

> **GUARDRAIL**: All data must come from Grafana/Loki or GitHub MCP tools only.
> Do NOT use Read, Glob, Grep, or Bash to access system files during investigation.
> The local workspace is reserved exclusively for writing and committing code fixes.

0. **VAGUENESS CHECK (MANDATORY — BEFORE ANY TOOL CALL)**
   Read the bug report above carefully. Does it contain:
   - A specific symptom (error message, HTTP code, stack trace, wrong behavior)?
   - An affected user/entity identifier (phone number, email, user ID, CR number, account)?
   - Steps to reproduce OR a clear description of what they were doing?

   If the report is just a vague statement like "can't onboard", "portal not working",
   "getting error", "payment failed" without specifics — **STOP HERE**.
   Do NOT call any tools. Instead:
   - Set `clarification_questions` to ask the reporter for:
     - What exactly they were doing step by step
     - What they saw (exact error message, blank page, wrong data, etc.)
     - Their identifier (phone number, email, CR number — whichever applies to the feature)
     - When it happened (date/time)
     - A screenshot if possible
   - Set `fix_type="unknown"`, `action="ask_reporter"`, `confidence=0.0`
   - Return immediately. Do NOT proceed to step 1.

   Only continue to step 1 if the report has at least a specific symptom AND
   either a user identifier or clear reproduction steps.

1. **Observability — Grafana Loki (MANDATORY FIRST STEP)** *(skill: logs_reading.md)*
   Grafana is running at http://localhost:3000 and Loki at http://localhost:3100.
   You MUST query Loki before doing anything else.

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
      - Construct a Grafana Explore deep-link:
        `http://localhost:3000/explore?orgId=1&left=%7B"datasource":"<UID>","queries":%5B%7B"expr":"<URL-encoded LogQL>","refId":"A"%7D%5D,"range":%7B"from":"now-1h","to":"now"%7D%7D`
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
   always LIMIT results) to check for data inconsistencies or unexpected values.

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
