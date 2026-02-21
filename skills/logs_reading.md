# Logs Reading & Execution

Use this guide to query logs from Grafana (Loki) for a specific service and time window.

> **GUARDRAIL**: Logs must ONLY be queried via `mcp__bugbot_tools__query_loki_logs`.
> Do NOT read log files from the local filesystem using Read, Bash, or Grep.

## Grafana / Loki

### Basic log query structure (LogQL)

```logql
{service="<service_name>", env="production"} |= "error" | json
```

Common filters:

| Goal | LogQL snippet |
|---|---|
| Errors only | `\|= "error"` or `\| json \| level="error"` |
| By trace/request ID | `\|= "<trace_id>"` |
| By user/account | `\|= "account_id=<id>"` |
| Exclude noise | `!= "health_check"` |
| Time range | Set `from` and `to` in the MCP call |

### MCP call pattern

```
grafana.query_loki(
  query: '{service="oxo-payment-api", env="production"} |= "Exception" | json',
  from: "2026-02-21T10:00:00Z",
  to:   "2026-02-21T10:30:00Z",
  limit: 100
)
```

Always set `limit` — default can return thousands of lines.

### Reading a log line

Key fields to extract from structured logs:
- `timestamp` — when did it happen?
- `level` / `severity` — error, warning, info?
- `message` / `msg` — human-readable description
- `exception` / `stack_trace` — root cause for errors
- `trace_id` / `correlation_id` — use to cross-reference requests across services
- `user_id` / `account_id` — scope of impact

---

## Execution Checklist

1. Always bound queries to a time window — unbounded queries can be slow and noisy.
2. Start broad (service-level error count), then narrow (specific endpoint/error class).
3. Extract `trace_id` from the first error and use it to pull the full request trace.
4. Record key findings — timestamp of first occurrence, error message, affected volume — before moving to root cause.
5. Never modify log configurations or alert rules during an investigation.
