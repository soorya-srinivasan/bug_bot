# PRD: OnCall Management Service

**Version:** 1.0
**Date:** 2026-02-21
**Status:** Draft

---

## 1. Overview

### 1.1 Problem Statement

Engineering organizations need a centralized system to manage on-call responsibilities across teams and services. Without one, on-call knowledge lives in spreadsheets, Slack pinned messages, or tribal memory — leading to missed escalations, unclear ownership, and unfair workload distribution.

### 1.2 Objective

Build an **OnCall Management Service** that provides:
- A single source of truth for team ownership of deployed services
- Scheduled on-call rotations with automatic updates
- Manual swap/override capabilities
- Full audit trail of every change
- Slack notifications for all on-call transitions

### 1.3 Target Users

| Role | Usage |
|------|-------|
| **Engineering Managers** | Configure teams, assign services, set rotation policies |
| **On-Call Engineers** | View their schedule, request swaps, acknowledge shifts |
| **Incident Responders** | Look up who is on-call for a given service right now |
| **Platform/SRE Teams** | Integrate on-call data into alerting and incident pipelines |

### 1.4 Interfaces

- **Web Dashboard** — Admin UI for managing teams, services, schedules, and viewing audit logs
- **REST API** — Programmatic access for integrations (alerting tools, chatbots, incident management)
- **Slack Notifications** — DM and channel-based alerts for on-call transitions

---

## 2. Core Concepts & Domain Model

### 2.1 Entity Relationships

```
Organization (tenant)
 └── Team
      ├── Members[] (users belonging to this team)
      ├── Services[] (deployed services owned by this team)
      │    ├── service_owner (default: inherits from team)
      │    └── team (always mapped)
      ├── RotationConfig (how on-call auto-rotates)
      ├── OnCallSchedule[] (planned shifts)
      ├── OnCallOverride[] (temporary swaps)
      └── AuditLog[] (every change, ever)
```

### 2.2 Entity Definitions

#### 2.2.1 Organization

Multi-tenant root. All data is scoped to an organization.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `name` | string | Organization name |
| `slug` | string | URL-safe identifier (unique) |
| `slack_workspace_id` | string | Connected Slack workspace |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

#### 2.2.2 User

A person who can be on-call or administer the system.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `org_id` | UUID | FK → Organization |
| `email` | string | Unique within org |
| `display_name` | string | Human-readable name |
| `slack_user_id` | string | Slack member ID (for notifications) |
| `role` | enum | `admin`, `manager`, `member` |
| `timezone` | string | IANA timezone (e.g., `Asia/Kolkata`) |
| `is_active` | boolean | Soft-delete flag |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

#### 2.2.3 Team

A group of engineers who share on-call responsibility.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `org_id` | UUID | FK → Organization |
| `name` | string | Team name (unique within org) |
| `slug` | string | URL-safe identifier |
| `description` | text | Optional team description |
| `slack_channel_id` | string | Team's Slack channel for notifications |
| `current_oncall_user_id` | UUID | FK → User (resolved current on-call) |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

**Relationships:**
- `members` → many-to-many with User via `team_memberships`
- `services` → one-to-many with Service
- `rotation_config` → one-to-one with RotationConfig
- `schedules` → one-to-many with OnCallSchedule
- `overrides` → one-to-many with OnCallOverride

#### 2.2.4 Team Membership

Join table linking users to teams with role context.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `team_id` | UUID | FK → Team |
| `user_id` | UUID | FK → User |
| `team_role` | enum | `lead`, `member` |
| `is_eligible_for_oncall` | boolean | Can this member be scheduled? (default: true) |
| `weight` | float | Weighted rotation factor (default: 1.0). Higher = more shifts. |
| `joined_at` | timestamp | |

**Constraints:** Unique on `(team_id, user_id)`

#### 2.2.5 Service

A deployed service (microservice, monolith module, etc.) owned by a team.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `org_id` | UUID | FK → Organization |
| `name` | string | Service name (unique within org) |
| `description` | text | What this service does |
| `repository_url` | string | Source code repo URL |
| `team_id` | UUID | FK → Team (owning team) |
| `owner_user_id` | UUID | FK → User (service owner / tech lead) |
| `environment` | string | `production`, `staging`, etc. |
| `tier` | enum | `critical`, `standard`, `low` — affects escalation urgency |
| `metadata` | jsonb | Extensible key-value pairs (runbook URL, dashboard link, etc.) |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

**Default behavior:** When a service is created and `owner_user_id` is null, it defaults to the team lead. The team mapping is **required** — every service must belong to exactly one team.

#### 2.2.6 Rotation Config

Per-team configuration for automatic on-call rotation.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `team_id` | UUID | FK → Team (unique) |
| `enabled` | boolean | Is auto-rotation active? |
| `strategy` | enum | `round_robin`, `custom_order`, `weighted` |
| `rotation_interval` | enum | `daily`, `weekly`, `biweekly` |
| `handoff_day` | int | Day of week for handoff (0=Mon, 6=Sun). For weekly/biweekly. |
| `handoff_time` | time | Time of day for handoff (in UTC) |
| `custom_order` | jsonb | Ordered list of user_ids (for `custom_order` strategy) |
| `current_index` | int | Current position in the rotation sequence |
| `effective_from` | date | When this config takes effect |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

**Strategy Details:**

| Strategy | Behavior |
|----------|----------|
| `round_robin` | Cycles through all on-call-eligible team members alphabetically by name. Equal distribution. |
| `custom_order` | Cycles through `custom_order` list in exact sequence. Allows skipping members or repeating. |
| `weighted` | Uses `team_memberships.weight` to distribute shifts proportionally. Higher weight = more frequent shifts. Algorithm tracks cumulative assignments to maintain target ratios over time. |

#### 2.2.7 OnCall Schedule

A planned on-call shift for a specific engineer.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `team_id` | UUID | FK → Team |
| `user_id` | UUID | FK → User (the on-call engineer) |
| `start_time` | timestamp | Shift start (inclusive) |
| `end_time` | timestamp | Shift end (exclusive) |
| `schedule_type` | enum | `auto` (generated by rotation) or `manual` (created by a human) |
| `created_by` | UUID | FK → User who created this entry |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

**Constraints:**
- No overlapping schedules within the same team (enforced at DB + application layer)
- `start_time < end_time`
- `user_id` must be a member of `team_id`

#### 2.2.8 OnCall Override

A temporary swap or substitute for a scheduled shift. Overrides always take precedence over regular schedules.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `team_id` | UUID | FK → Team |
| `original_user_id` | UUID | FK → User (who was originally scheduled) |
| `substitute_user_id` | UUID | FK → User (who is covering) |
| `start_time` | timestamp | Override start (inclusive) |
| `end_time` | timestamp | Override end (exclusive) |
| `reason` | text | Why the swap is happening |
| `status` | enum | `pending`, `approved`, `rejected`, `cancelled` |
| `requested_by` | UUID | FK → User who initiated the swap |
| `approved_by` | UUID | FK → User who approved (nullable, for approval workflows) |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

**Constraints:**
- No overlapping overrides for the same team
- `substitute_user_id` must be a member of `team_id`

#### 2.2.9 Audit Log

Immutable record of every mutation in the system.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID | Primary key |
| `org_id` | UUID | FK → Organization |
| `entity_type` | string | `team`, `service`, `schedule`, `override`, `rotation_config`, `user`, `team_membership` |
| `entity_id` | UUID | ID of the affected entity |
| `action` | enum | `created`, `updated`, `deleted`, `rotation_triggered`, `override_approved`, `override_rejected`, `swap_requested`, `handoff_completed` |
| `actor_id` | UUID | FK → User who performed the action (null for system actions) |
| `actor_type` | enum | `user`, `system`, `api_key` |
| `changes` | jsonb | Diff of what changed: `{ "field": { "old": X, "new": Y } }` |
| `metadata` | jsonb | Additional context (IP address, user agent, API key name, etc.) |
| `created_at` | timestamp | Immutable — never updated |

**Properties:**
- Append-only — no updates or deletes permitted
- Indexed on `(entity_type, entity_id)`, `(org_id, created_at)`, `(actor_id)`
- Retention policy configurable per org (default: 2 years)

---

## 3. Features & Requirements

### 3.1 Team Management

| ID | Requirement | Priority |
|----|-------------|----------|
| TM-1 | Create, read, update, delete teams | P0 |
| TM-2 | Add/remove members to/from teams | P0 |
| TM-3 | Assign team roles (`lead`, `member`) | P0 |
| TM-4 | Mark members as eligible/ineligible for on-call | P1 |
| TM-5 | Set per-member weight for weighted rotation | P1 |
| TM-6 | Link a Slack channel to a team for notifications | P0 |
| TM-7 | View team roster with current on-call highlighted | P0 |

### 3.2 Service Management

| ID | Requirement | Priority |
|----|-------------|----------|
| SV-1 | Register a service with name, description, repo URL, tier | P0 |
| SV-2 | Assign a service to exactly one team (required) | P0 |
| SV-3 | Assign a service owner (defaults to team lead if unset) | P0 |
| SV-4 | Transfer service ownership between teams | P1 |
| SV-5 | Store extensible metadata (runbook URL, dashboard link, alerting config) | P2 |
| SV-6 | Look up "who is on-call for service X right now?" | P0 |
| SV-7 | Bulk import services via CSV/API | P2 |

### 3.3 On-Call Scheduling

| ID | Requirement | Priority |
|----|-------------|----------|
| SC-1 | Create manual on-call schedules (assign engineer to time range) | P0 |
| SC-2 | View current and upcoming schedules per team (calendar view) | P0 |
| SC-3 | Prevent overlapping schedules within a team | P0 |
| SC-4 | Auto-generate future schedules based on rotation config | P0 |
| SC-5 | Resolve "who is on-call right now?" with priority: override > schedule > fallback | P0 |
| SC-6 | Support daily, weekly, and biweekly rotation intervals | P0 |
| SC-7 | Configure handoff day and time per team | P1 |
| SC-8 | Generate schedules N weeks into the future (configurable lookahead) | P1 |
| SC-9 | Notify outgoing and incoming engineer on handoff | P0 |

### 3.4 On-Call Overrides & Swaps

| ID | Requirement | Priority |
|----|-------------|----------|
| OV-1 | Request an override (swap with another team member for a time range) | P0 |
| OV-2 | Override approval workflow (optional — can be auto-approved or require manager approval) | P1 |
| OV-3 | Cancel a pending or approved override | P0 |
| OV-4 | Overrides always take precedence over regular schedules | P0 |
| OV-5 | Notify affected parties (original, substitute, team channel) on override creation/approval | P0 |
| OV-6 | Prevent overlapping overrides within a team | P0 |

### 3.5 Rotation Engine

| ID | Requirement | Priority |
|----|-------------|----------|
| RT-1 | Round-robin rotation through eligible team members | P0 |
| RT-2 | Custom-order rotation with explicit user sequence | P0 |
| RT-3 | Weighted rotation — distribute shifts proportionally to member weights | P1 |
| RT-4 | Configurable rotation interval (daily / weekly / biweekly) | P0 |
| RT-5 | Configurable handoff day and time | P1 |
| RT-6 | Skip ineligible members (vacation, `is_eligible_for_oncall = false`) | P1 |
| RT-7 | Automatic schedule generation job (runs periodically, fills N weeks ahead) | P0 |
| RT-8 | Manual trigger to regenerate future schedules after config change | P1 |
| RT-9 | Rotation respects existing overrides — does not overwrite approved overrides | P0 |

**Weighted Rotation Algorithm:**

```
For each eligible member:
  target_ratio = member.weight / sum(all_weights)
  actual_ratio = member.shifts_completed / total_shifts_completed

Assign next shift to the member with the largest gap:
  priority = target_ratio - actual_ratio
  select member with highest priority (ties broken by last_oncall_date ascending)
```

### 3.6 Notifications (Slack)

| ID | Requirement | Priority |
|----|-------------|----------|
| NT-1 | DM the incoming on-call engineer when their shift starts | P0 |
| NT-2 | DM the outgoing on-call engineer when their shift ends | P1 |
| NT-3 | Post to team's Slack channel on every on-call transition | P0 |
| NT-4 | Notify when an override is requested, approved, or rejected | P0 |
| NT-5 | Weekly summary posted to team channel (upcoming schedule) | P2 |
| NT-6 | Configurable notification preferences per team (enable/disable specific notifications) | P2 |

### 3.7 Audit Trail

| ID | Requirement | Priority |
|----|-------------|----------|
| AU-1 | Log every create, update, delete across all entities | P0 |
| AU-2 | Log automatic rotation events with `actor_type = system` | P0 |
| AU-3 | Store field-level diffs in `changes` column | P0 |
| AU-4 | Log override approvals/rejections with approver info | P0 |
| AU-5 | Audit log is immutable — no updates or deletes | P0 |
| AU-6 | Filter audit logs by entity, actor, action, date range | P0 |
| AU-7 | Export audit logs (CSV/JSON) | P2 |
| AU-8 | Configurable retention policy per organization | P2 |

### 3.8 Web Dashboard

| ID | Requirement | Priority |
|----|-------------|----------|
| UI-1 | Team management page (CRUD, member list, current on-call) | P0 |
| UI-2 | Service directory page (list services, filter by team/tier, view owner) | P0 |
| UI-3 | Schedule calendar view per team (visual timeline of who is on-call when) | P0 |
| UI-4 | Override management (request, approve, cancel) | P0 |
| UI-5 | Rotation config page per team (strategy, interval, handoff settings) | P0 |
| UI-6 | Audit log viewer with filters and search | P1 |
| UI-7 | "Who is on-call?" global search (by service name or team name) | P0 |
| UI-8 | Dashboard home — summary of active on-calls across all teams | P1 |

---

## 4. API Design

### 4.1 Resource Endpoints

All endpoints are scoped to the authenticated organization.

#### Teams
```
GET    /api/v1/teams                          # List teams (paginated, filterable)
POST   /api/v1/teams                          # Create team
GET    /api/v1/teams/{team_id}                # Get team details
PATCH  /api/v1/teams/{team_id}                # Update team
DELETE /api/v1/teams/{team_id}                # Delete team (soft)

POST   /api/v1/teams/{team_id}/members        # Add member
PATCH  /api/v1/teams/{team_id}/members/{user_id}  # Update member (role, weight, eligibility)
DELETE /api/v1/teams/{team_id}/members/{user_id}   # Remove member
```

#### Services
```
GET    /api/v1/services                       # List services (filterable by team, tier)
POST   /api/v1/services                       # Register service
GET    /api/v1/services/{service_id}          # Get service details
PATCH  /api/v1/services/{service_id}          # Update service
DELETE /api/v1/services/{service_id}          # Deregister service (soft)
GET    /api/v1/services/{service_id}/oncall   # Who is on-call for this service?
```

#### On-Call Schedules
```
GET    /api/v1/teams/{team_id}/schedules      # List schedules (filterable by date range)
POST   /api/v1/teams/{team_id}/schedules      # Create manual schedule
GET    /api/v1/teams/{team_id}/schedules/{id} # Get schedule details
PATCH  /api/v1/teams/{team_id}/schedules/{id} # Update schedule
DELETE /api/v1/teams/{team_id}/schedules/{id} # Delete schedule

GET    /api/v1/teams/{team_id}/oncall         # Who is on-call for this team right now?
GET    /api/v1/teams/{team_id}/oncall?at={timestamp}  # Who is on-call at a specific time?
```

#### On-Call Overrides
```
GET    /api/v1/teams/{team_id}/overrides      # List overrides
POST   /api/v1/teams/{team_id}/overrides      # Request override
GET    /api/v1/teams/{team_id}/overrides/{id} # Get override details
PATCH  /api/v1/teams/{team_id}/overrides/{id} # Update (approve/reject/cancel)
DELETE /api/v1/teams/{team_id}/overrides/{id} # Cancel override
```

#### Rotation Config
```
GET    /api/v1/teams/{team_id}/rotation       # Get rotation config
PUT    /api/v1/teams/{team_id}/rotation       # Create or replace rotation config
PATCH  /api/v1/teams/{team_id}/rotation       # Update rotation config
POST   /api/v1/teams/{team_id}/rotation/generate  # Trigger schedule generation
POST   /api/v1/teams/{team_id}/rotation/preview   # Preview next N rotations without saving
```

#### Audit Logs
```
GET    /api/v1/audit-logs                     # List audit logs (filterable)
GET    /api/v1/audit-logs/export              # Export as CSV/JSON
```

#### Lookup (convenience)
```
GET    /api/v1/oncall?service={name}          # Global: who is on-call for a service?
GET    /api/v1/oncall?team={name}             # Global: who is on-call for a team?
GET    /api/v1/users/{user_id}/schedules      # All upcoming schedules for a user across teams
```

### 4.2 Authentication & Authorization

| Role | Permissions |
|------|-------------|
| `admin` | Full CRUD on all resources. Manage org settings. |
| `manager` | CRUD on teams they lead. Approve overrides. Configure rotation. |
| `member` | View schedules. Request overrides. View own audit logs. |

API authentication via Bearer token (JWT or API key). All API responses include `X-Request-Id` for traceability.

---

## 5. On-Call Resolution Logic

When a consumer asks "who is on-call for team/service X?", the system resolves in this priority order:

```
1. Active Override   → Is there an approved override covering the current time?
                        YES → return override.substitute_user_id

2. Active Schedule   → Is there a schedule entry covering the current time?
                        YES → return schedule.user_id

3. Fallback          → Return team.current_oncall_user_id (last known on-call)

4. Service Owner     → If queried by service and no team-level on-call found,
                        return service.owner_user_id

5. Team Lead         → Last resort: return the team lead
```

Each resolution step is logged with the source (`override`, `schedule`, `fallback`, `service_owner`, `team_lead`) so consumers know how the result was determined.

---

## 6. Rotation Engine (Scheduler)

### 6.1 Execution

A background job runs on a configurable interval (default: every hour) and:

1. Queries all teams where `rotation_config.enabled = true`
2. For each team, checks if it's time for a handoff (based on `handoff_day`, `handoff_time`, `rotation_interval`)
3. If a handoff is due:
   a. Determines the next engineer based on the active strategy
   b. Creates a new `OnCallSchedule` entry
   c. Updates `team.current_oncall_user_id`
   d. Sends Slack notifications
   e. Logs to `AuditLog` with `action = rotation_triggered`, `actor_type = system`
4. Optionally generates schedules N periods into the future (lookahead)

### 6.2 Schedule Generation (Lookahead)

When rotation config changes or is first enabled:
- The system generates schedules for the next `N` periods (configurable, default: 4 weeks)
- Future schedules are marked `schedule_type = auto`
- If a rotation config changes, all future `auto` schedules are deleted and regenerated
- Existing `manual` schedules and approved `overrides` are never touched

### 6.3 Idempotency

The rotation engine is idempotent — running it multiple times for the same handoff period produces the same result. It checks if a schedule already exists for the target period before creating a new one.

---

## 7. Slack Integration

### 7.1 Notifications

| Event | Channel | Message |
|-------|---------|---------|
| Shift started | DM to incoming engineer | "You are now on-call for **{team}** until {end_time}" |
| Shift ended | DM to outgoing engineer | "Your on-call shift for **{team}** has ended. {next_engineer} is now on-call." |
| Handoff | Team channel | ":arrows_counterclockwise: On-call handoff: **{outgoing}** -> **{incoming}** for {team}" |
| Override requested | DM to substitute + team channel | "{requester} requested {substitute} to cover on-call for {team} on {date}" |
| Override approved | DM to requester + substitute | "Override approved: {substitute} will cover {team} from {start} to {end}" |
| Override rejected | DM to requester | "Override request rejected by {approver}: {reason}" |

### 7.2 Interactive Messages (Future — P2)

- Swap request buttons (Accept / Decline)
- "I'm going to be unavailable" quick action
- Weekly schedule summary with "Request Swap" buttons

---

## 8. Audit Trail Design

### 8.1 What Gets Audited

Every state mutation is logged:

| Entity | Actions Audited |
|--------|----------------|
| Team | created, updated, deleted |
| Team Membership | member_added, member_removed, role_changed, weight_changed, eligibility_changed |
| Service | created, updated, deleted, ownership_transferred |
| OnCall Schedule | created, updated, deleted |
| OnCall Override | requested, approved, rejected, cancelled |
| Rotation Config | created, updated, rotation_triggered |
| User | created, updated, deactivated |

### 8.2 Audit Log Entry Structure

```json
{
  "id": "uuid",
  "org_id": "uuid",
  "entity_type": "schedule",
  "entity_id": "uuid",
  "action": "created",
  "actor_id": "uuid",
  "actor_type": "user",
  "changes": {
    "user_id": { "old": null, "new": "uuid-of-engineer" },
    "start_time": { "old": null, "new": "2026-03-01T09:00:00Z" },
    "end_time": { "old": null, "new": "2026-03-08T09:00:00Z" }
  },
  "metadata": {
    "ip_address": "10.0.1.42",
    "user_agent": "Mozilla/5.0...",
    "source": "web_dashboard"
  },
  "created_at": "2026-02-21T14:30:00Z"
}
```

### 8.3 Querying

Audit logs support filtering by:
- `entity_type` + `entity_id` — "show me all changes to this team"
- `actor_id` — "show me everything this person did"
- `action` — "show me all rotation triggers"
- `created_at` range — "show me changes in the last 7 days"
- Full-text search on `changes` JSON (for finding specific field mutations)

---

## 9. Data Integrity & Constraints

| Rule | Enforcement |
|------|-------------|
| Every service belongs to exactly one team | NOT NULL FK + application validation |
| No overlapping schedules per team | DB exclusion constraint + application check |
| No overlapping overrides per team | DB exclusion constraint + application check |
| Scheduled user must be a team member | Application validation on create/update |
| Override substitute must be a team member | Application validation on create/update |
| Audit logs are immutable | No UPDATE/DELETE permissions on audit table |
| Rotation config is 1:1 with team | Unique constraint on `team_id` |
| Soft deletes for teams, services, users | `is_active` / `deleted_at` flag, never hard delete |

---

## 10. Non-Functional Requirements

| Category | Requirement |
|----------|-------------|
| **Availability** | 99.9% uptime — on-call resolution is critical for incident response |
| **Latency** | "Who is on-call?" queries < 100ms p99 |
| **Scalability** | Support 500+ teams, 10,000+ services, 50,000+ schedule entries |
| **Data Retention** | Audit logs retained for 2 years (configurable) |
| **Security** | Role-based access control. API key rotation. Audit log tamper protection. |
| **Backup** | Daily automated DB backups with point-in-time recovery |
| **Observability** | Structured logging, request tracing, rotation job health metrics |

---

## 11. Edge Cases & Error Handling

| Scenario | Behavior |
|----------|----------|
| All team members marked ineligible | Rotation skipped. Alert sent to team channel + team lead. Audit logged. |
| Override requested for time with no schedule | Allowed — override creates implicit coverage. |
| Team deleted with active schedules | Soft delete team. Cancel all future `auto` schedules. Keep history. |
| Service transferred to new team | Old team's on-call no longer applies. New team's on-call takes effect immediately. Audit logged. |
| Rotation config changed mid-cycle | Future `auto` schedules regenerated. Current active schedule runs to completion. |
| Duplicate rotation trigger (idempotency) | No-op if schedule already exists for the period. |
| User removed from team while on-call | Current shift runs to completion. No future schedules generated. Alert to team lead. |

---

## 12. Future Considerations (Out of Scope for v1)

| Feature | Description |
|---------|-------------|
| **Escalation Policies** | Multi-tier escalation (primary → secondary → manager) with configurable timeout |
| **Follow-the-Sun** | Timezone-aware rotation for globally distributed teams |
| **PagerDuty / Opsgenie sync** | Bidirectional sync with external on-call tools |
| **Vacation calendar integration** | Auto-mark members ineligible during PTO (Google Calendar, BambooHR) |
| **On-call compensation tracking** | Track hours served for compensation or time-off-in-lieu |
| **Mobile app** | Push notifications and schedule management on mobile |
| **Slack slash commands** | `/oncall who {service}`, `/oncall swap @user {date}` |
| **Webhooks** | Outbound webhooks on on-call transitions for external integrations |

---

## 13. Success Metrics

| Metric | Target |
|--------|--------|
| Time to resolve "who is on-call?" | < 5 seconds (manual lookup → instant API/UI) |
| On-call coverage gaps | 0 unscheduled gaps for teams with rotation enabled |
| Override fulfillment rate | > 90% of swap requests approved within 4 hours |
| Audit completeness | 100% of mutations logged with actor and diff |
| Adoption | 80% of engineering teams onboarded within 3 months |

---

## 14. Glossary

| Term | Definition |
|------|------------|
| **Handoff** | The transition of on-call responsibility from one engineer to another |
| **Override** | A temporary substitution where one engineer covers for another |
| **Rotation** | The automated cycling of on-call responsibility across team members |
| **Schedule** | A planned assignment of an engineer to an on-call time window |
| **Shift** | A single continuous period of on-call duty for one engineer |
| **Tier** | Service criticality level affecting escalation urgency |
| **Weight** | A multiplier controlling how often a member is scheduled in weighted rotation |
