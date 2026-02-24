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

**Full example — keyword search over a natural-language time window:**
```
mcp__bugbot_tools__query_loki_logs(
  query:           '{service="oxo-payment-api", env="production"} | json',
  keywords:        ["international", "payment", "error"],
  time_expression: "last 2 hours",
  limit:           200
)
```
The tool builds `|~ "(?i)international|payment|error"` automatically, runs it, reports
per-keyword hit counts, and retries any keyword with zero hits individually.

**time_expression values (resolved server-side from current local time):**

| What reporter says          | Pass as time_expression       |
|-----------------------------|-------------------------------|
| "last 2 hours"              | `"last 2 hours"`              |
| "last 30 minutes"           | `"last 30 minutes"`           |
| "last N days"               | `"last N days"`               |
| "yesterday"                 | `"yesterday"`                 |
| "today"                     | `"today"`                     |

**Absolute time range (when reporter gives a specific clock time):**
```
mcp__bugbot_tools__query_loki_logs(
  query:     '{service="oxo-payment-api", env="production"} | json',
  keywords:  ["payment", "timeout"],
  from_time: "2026-02-21T14:00:00",   # naive → server local timezone
  to_time:   "2026-02-21T14:30:00",
  limit:     100
)
```
Explicit UTC offset also accepted: `"2026-02-21T14:00:00+05:30"`.

**Relative fallback (no time context):**
```
mcp__bugbot_tools__query_loki_logs(
  query:                '{service="oxo-payment-api"} | json',
  keywords:             ["exception"],
  start_offset_minutes: 60,
  limit:                100
)
```

Always set `limit` — default can return thousands of lines.

### Timezone rules

- Returned timestamps are always in the **server's local timezone** (shown in the result header).
- `time_expression` strings are resolved against the server clock — never compute the range manually.
- For specific clock times ("around 2 PM"), use `from_time`/`to_time` with ±15 min buffer.

### Time coverage checklist

Before concluding there are no relevant logs:
1. Try every label selector variant (`service`, `app`, `job`, bare `{}`).
2. If still empty, widen: `start_offset_minutes=360`, then `start_offset_minutes=1440`.
3. Only after exhausting all selectors AND time widths should you record "no logs found".

### Keyword coverage checklist

- Always pass `keywords` as a list of individual terms from the bug description.
- The tool's coverage report shows `[✓]` / `[✗]` per keyword — check it before proceeding.
- Any `[✗]` keyword is automatically retried; its individual results appear in a separate section.

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
2. If the reporter mentions a specific time, use `from_time`/`to_time` with ±15 min buffer
   (naive times are local timezone). Otherwise start with `start_offset_minutes=60`.
3. If no results: try all label selector fallbacks, then widen to 360 min, then 1440 min.
4. Start broad (service-level error count), then narrow (specific endpoint/error class).
5. Extract `trace_id` from the first error and use it to pull the full request trace.
6. Record key findings — timestamp of first occurrence, error message, affected volume — before moving to root cause.
7. Never modify log configurations or alert rules during an investigation.
