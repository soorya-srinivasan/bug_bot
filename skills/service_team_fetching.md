# Service Team Fetching

When a bug report references a service name, use this guide to identify the owning team, their repo, and escalation contacts.

## Step 1 — Resolve Service to Team

Call the `lookup_service_owner` tool with the service name:

```
lookup_service_owner(service: "<service_name>")
```

Expected response fields:
- `team_name` — the owning squad (e.g. "payments-team", "inventory-team")
- `repo` — GitHub repository slug (e.g. "shopuptech/oxo-payment-api")
- `slack_channel` — primary contact channel
- `oncall` — current on-call engineer (if available)

## Step 2 — Confirm via GitHub CODEOWNERS

If `lookup_service_owner` returns no result, fall back to GitHub:

```
github.search_code(query: "CODEOWNERS", repo: "<org>/<repo>")
```

Parse the CODEOWNERS file to identify the team with ownership of the relevant path.

## Step 3 — Map Service to Platform

| Service Prefix | Platform | Repo Pattern |
|---|---|---|
| Payment, Bill, Subscription | OXO.APIs (.NET) | `shopuptech/oxo-*-api` |
| Inventory, Auth, Company | OXO.APIs (.NET) | `shopuptech/oxo-*-api` |
| AFT, audit, vconnect-* | vconnect (Rails) | `shopuptech/vconnect` |

## Step 4 — Escalation

If the bug is critical (P0/P1) and no on-call is found:
1. Note the team name and Slack channel in your investigation summary.
2. Recommend the reporter page the on-call via PagerDuty for that team.
3. Do NOT attempt a fix without team confirmation on P0 incidents.
