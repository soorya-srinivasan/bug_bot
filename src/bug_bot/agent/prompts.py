def build_investigation_prompt(
    bug_id: str,
    description: str,
    severity: str,
    relevant_services: list[str],
) -> str:
    services_str = ", ".join(relevant_services) if relevant_services else "unknown"

    return f"""Investigate the following bug report and provide a structured analysis.

## Bug Report
- **Bug ID:** {bug_id}
- **Severity:** {severity}
- **Potentially Affected Services:** {services_str}

## Report Content
{description}

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
- Use the `lookup_service_owner` tool to find repo and team info for services.
- Use the `report_finding` tool to log significant observations during investigation.
- Be thorough but time-efficient. Focus on the most likely causes first."""
