# Investigation Planning

Before diving into logs or code, build a structured plan. This prevents wasted tool calls and keeps the investigation focused.

## Planning Template

For every bug report, answer these questions first:

```
1. What is the reported symptom?          (e.g. "Payment service returning 500")
2. When did it start?                     (timestamp / deployment window)
3. Which service(s) are involved?         (use service_team_fetching.md)
4. What is the blast radius?              (single user, subset, all users?)
5. Is there a known recent change?        (deployment, config, migration)
```

## Investigation Phases

### Phase 1 — Triage (always first)
- Check Grafana error-rate panel for the affected service
- Confirm whether the issue is ongoing or resolved
- See `logs_reading.md` for Loki query patterns

### Phase 2 — Isolate
- Narrow to the specific endpoint / job / worker triggering the error
- Pull the first occurrence timestamp from Loki
- Identify whether the error is consistent or intermittent

### Phase 3 — Culprit Commit
Identify the exact commit that introduced the regression:

1. Get the **first error timestamp** from Loki logs
2. List merged PRs/commits on the service repo around that time:
   ```
   github.list_commits(repo: "shopuptech/<repo>", until: "<first_error_timestamp>", per_page: 20)
   ```
3. Find the **last green commit** (just before errors started) and the **first bad commit** (when errors began)
4. Diff the bad commit to confirm it touches the suspected code path:
   ```
   github.compare(repo: "shopuptech/<repo>", base: "<last_good_sha>", head: "<first_bad_sha>")
   ```
5. Record the culprit commit SHA and PR number in your findings before proceeding to a fix

### Phase 4 — Root Cause
- Read the culprit commit diff via GitHub (do not clone unless necessary)
- Query the database read-only if data corruption is suspected

### Phase 5 — Fix or Escalate
- If root cause is clear and fix is low-risk → proceed to `pr_execution.md`
- If root cause is unclear or fix is high-risk → escalate with full findings
- If data fix is needed → provide the SQL/steps and recommend DBA execution

## Output Format

At the end of planning, produce:

```
## Bug Investigation Plan
- Bug ID: <id>
- Service: <name> (Team: <team>)
- Hypothesis: <one sentence>
- Next step: <Phase N — specific action>
```
