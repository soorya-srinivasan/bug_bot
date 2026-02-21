# Implementation Plan: Align OnCall Management to PRD

## Context

The oncall management system has a working foundation (team CRUD, schedules, overrides, round-robin/custom rotation via Temporal, Slack notifications, history tracking) across both the bug_bot backend and firstline-ai frontend. However, the PRD (`plans/oncall_prd_plan.md`) defines a significantly richer feature set. This plan bridges the gap.

### Architectural Decisions (confirmed with user)
- **No multi-tenancy** — skip Organization entity
- **Keep Slack IDs as identity** — no User table
- **Keep rotation config embedded in Team** — add missing fields to existing model
- **Keep date-based** schedules/overrides — no timestamp migration
- **Slack as source + DB metadata** for team membership — lightweight TeamMembership table for oncall-specific attributes (role, weight, eligibility)
- **Extend OnCallHistory → AuditLog** — rename table, add entity_type/entity_id/changes/metadata columns

---

## Phase 1: Database Schema & Model Layer

**Goal**: Add all new columns and tables. No behavioral changes — existing code continues to work.

### 1.1 Migration: Team Model Additions

Add to `teams` table:
| Column | Type | Notes |
|--------|------|-------|
| `name` | String(100), NOT NULL | Display name. Backfill from `slack_group_id`. |
| `slug` | String(100), UNIQUE, NOT NULL | URL-safe. Backfill from `slack_group_id`. |
| `description` | Text, nullable | |
| `slack_channel_id` | String(30), nullable | For team channel notifications (distinct from `slack_group_id`) |
| `rotation_interval` | String(10), default `'weekly'` | `'daily'` \| `'weekly'` \| `'biweekly'` |
| `handoff_day` | Integer, nullable | 0=Mon, 6=Sun |
| `handoff_time` | Time, nullable | UTC |
| `is_active` | Boolean, NOT NULL, default `true` | Soft delete flag |

Extend `rotation_type` allowed values to include `'weighted'` (application-level, no ALTER needed).

### 1.2 Migration: TeamMembership Table (NEW)

```
team_memberships
├── id                      UUID PK
├── team_id                 UUID FK→teams.id ON DELETE CASCADE
├── slack_user_id           String(20) NOT NULL
├── team_role               String(10) default 'member'  — 'lead' | 'member'
├── is_eligible_for_oncall  Boolean default true
├── weight                  Float default 1.0
├── joined_at               DateTime(tz) default now()
└── UNIQUE(team_id, slack_user_id)
```

Slack group remains the member list source. This table stores oncall-specific metadata only. Members in Slack but not in this table get defaults when queried.

### 1.3 Migration: Service Model Additions

Add to `service_team_mapping` table:
| Column | Type | Notes |
|--------|------|-------|
| `repository_url` | String(500), nullable | Backfill from `github_repo` |
| `environment` | String(50), nullable | `'production'`, `'staging'`, etc. |
| `tier` | String(20), nullable | `'critical'` \| `'standard'` \| `'low'` |
| `metadata` | JSONB, nullable | Extensible key-value (runbook URL, dashboard link) |
| `is_active` | Boolean, NOT NULL, default `true` | Soft delete flag |

### 1.4 Migration: Schedule & Override Additions

**`oncall_schedules`** — add:
- `origin` String(10), NOT NULL, default `'manual'` — `'auto'` (rotation-generated) | `'manual'` (human-created)

**`oncall_overrides`** — add:
- `status` String(20), NOT NULL, default `'approved'` — `'pending'` | `'approved'` | `'rejected'` | `'cancelled'`
- `requested_by` String(20), nullable
- `approved_by` String(20), nullable

Backfill: existing overrides get `status='approved'`, `requested_by=created_by`.

### 1.5 Migration: Audit Log Table (NEW)

```
oncall_audit_logs
├── id                          UUID PK
├── team_id                     UUID FK→teams.id, nullable
├── entity_type                 String(30) NOT NULL  — 'team'|'service'|'schedule'|'override'|'rotation_config'|'team_membership'
├── entity_id                   UUID NOT NULL
├── action                      String(30) NOT NULL  — 'created'|'updated'|'deleted'|'rotation_triggered'|'override_approved'|...
├── actor_type                  String(10) default 'user'  — 'user'|'system'
├── actor_id                    String(20), nullable  — Slack user ID, null for system
├── changes                     JSONB, nullable       — {"field": {"old": X, "new": Y}}
├── metadata                    JSONB, nullable       — source, IP, etc.
├── engineer_slack_id           String(20), nullable  — legacy compat
├── previous_engineer_slack_id  String(20), nullable  — legacy compat
├── change_type                 String(20), nullable  — legacy compat
├── change_reason               Text, nullable        — legacy compat
├── effective_date              Date, nullable         — legacy compat
├── created_at                  DateTime(tz) default now()
└── Indexes: (entity_type, entity_id), (team_id), (action), (created_at)
```

Strategy: Dual-write to both `oncall_history` and `oncall_audit_logs` during Phase 1-3. Retire old table in Phase 4.

### 1.6 Update SQLAlchemy Models

**File**: `src/bug_bot/models/models.py`
- Add new columns to `Team` class
- Add `TeamMembership` class
- Update `ServiceTeamMapping` with new columns
- Add `origin` to `OnCallSchedule`
- Add `status`, `requested_by`, `approved_by` to `OnCallOverride`
- Add `OnCallAuditLog` class
- Add `memberships` relationship to Team

### Verification
- `alembic upgrade head` succeeds
- Existing tests pass unchanged
- Existing API responses still work (new fields have defaults)

---

## Phase 2: Backend Logic — Repository, Rotation Engine, Service Layer

**Goal**: Implement all new repository methods, upgrade rotation engine, add audit logging, and enhance Slack notifications.

### 2.1 Repository: Soft Deletes & New Team Fields

**File**: `src/bug_bot/db/repository.py`
- `list_teams()` — add `is_active=True` default filter
- `delete_team()` — change to soft delete (`is_active=False`)
- `create_team()` — auto-generate `slug` from `name`
- New: `get_team_by_slug(slug)`

### 2.2 Repository: TeamMembership CRUD

**File**: `src/bug_bot/db/repository.py`
- `list_team_memberships(team_id)` → list[TeamMembership]
- `upsert_team_membership(team_id, slack_user_id, data)` → TeamMembership
- `delete_team_membership(team_id, slack_user_id)`
- `get_eligible_members_for_rotation(team_id)` → members where `is_eligible_for_oncall=True`
- `merge_slack_members_with_db(team_id, slack_user_ids)` → merged list (DB metadata + defaults for missing)

### 2.3 Repository: Service Updates

**File**: `src/bug_bot/db/repository.py`
- `list_service_mappings()` — add `is_active`, `team_id`, `tier` filters
- `delete_service_mapping()` — soft delete
- New: `get_service_oncall(service_id)` — returns current on-call for a specific service

### 2.4 Repository: Override Status Transitions

**File**: `src/bug_bot/db/repository.py`
- New: `update_oncall_override(override_id, data)` — status transitions
- Update: `get_active_override_for_team()` — filter `status='approved'` only
- Update: `check_override_overlap()` — only check against non-cancelled/rejected overrides

### 2.5 Repository: Audit Log Methods

**File**: `src/bug_bot/db/repository.py`
- `create_oncall_audit_log(entity_type, entity_id, action, actor_type, actor_id, changes, metadata, team_id, **legacy_fields)`
- `list_oncall_audit_logs(*, entity_type, entity_id, action, actor_id, team_id, from_date, to_date, page, page_size)`
- Update `log_oncall_change()` to dual-write to both `oncall_history` AND `oncall_audit_logs`

### 2.6 Repository: Schedule Lookahead & Cross-Team Queries

**File**: `src/bug_bot/db/repository.py`
- `delete_future_auto_schedules(team_id, from_date)` — deletes `origin='auto'` schedules with `start_date >= from_date`
- `get_user_schedules(slack_user_id, from_date, to_date)` — all schedules across teams
- `global_oncall_lookup(*, service_name, team_name)` — convenience lookup

### 2.7 Rotation Engine Overhaul

**File**: `src/bug_bot/oncall/rotation.py`

**`should_rotate()`** — rewrite to support `rotation_interval`:
- `daily`: rotate every day
- `weekly`: every 7 days from `rotation_start_date`
- `biweekly`: every 14 days from `rotation_start_date`
- Check `handoff_day`: rotation only fires on the configured day of week

**`calculate_next_engineer()`** — add weighted strategy:
```
For each eligible member:
  target_ratio = member.weight / sum(all_weights)
  actual_ratio = shifts_completed / total_shifts (or 0 if first run)
  gap = target_ratio - actual_ratio
Select member with largest gap, break ties by longest since last oncall
```
- Accept `memberships` and `shift_counts` params for weighted mode
- Filter to eligible members only (from TeamMembership)

**`get_rotation_engineers()`** — accept `eligible_member_ids` filter param

**New `generate_schedule_lookahead()`**:
- Delete existing future auto schedules
- Simulate rotation for N periods using current strategy
- Return list of schedule dicts with `origin='auto'`

### 2.8 Service Layer Updates

**File**: `src/bug_bot/oncall/service.py`
- `assign_oncall()` — add `origin` parameter (default `'manual'`)
- `process_auto_rotation()` — fetch memberships for eligibility, compute shift counts for weighted, add idempotency check, generate lookahead, dual-write audit
- `get_current_oncall()` — filter overrides by `status='approved'`
- New: `preview_rotation(repo, team_id, weeks=4)` — simulate without persisting
- New: `generate_schedules(repo, team_id, weeks=4)` — force-generate auto schedules
- New: `approve_override(repo, override_id, approved_by)` / `reject_override(repo, override_id, approved_by)`

### 2.9 Slack Notifications Expansion

**File**: `src/bug_bot/oncall/slack_notifications.py`

New functions:
- `notify_team_channel_handoff(slack_channel_id, outgoing_id, incoming_id, effective_date)` — post to team channel
- `notify_outgoing_engineer(engineer_id, group_name, effective_date, incoming_id)` — DM outgoing
- `notify_override_request(requested_by_id, substitute_id, team_channel_id, override_date, reason)` — notify about pending override
- `notify_override_decision(requested_by_id, substitute_id, decision, decided_by_id)` — notify approval/rejection

Update `notify_oncall_rotation()` to also call channel and outgoing notifications.

### 2.10 Temporal Updates

**File**: `src/bug_bot/temporal/activities/database_activity.py`
- `fetch_rotation_enabled_teams()` — filter `is_active=True`
- `process_team_rotation()` — uses enhanced service layer (eligibility, weighted, lookahead)

**File**: `src/bug_bot/worker.py`
- Register any new activities

### Verification
- Unit tests: `should_rotate()` with daily/weekly/biweekly + handoff_day
- Unit tests: weighted rotation algorithm (verify convergence to weights over 20+ cycles)
- Unit tests: idempotency (duplicate rotation call = no duplicate schedule)
- Unit tests: `merge_slack_members_with_db()` (member in Slack but not DB gets defaults)
- Unit tests: override status transitions (valid + invalid)
- Integration: dual-write creates records in both `oncall_history` and `oncall_audit_logs`
- Integration: Temporal workflow processes rotations correctly with new logic

---

## Phase 3: API Endpoints & Pydantic Schemas

**Goal**: Add all new endpoints, update existing ones, update schemas.

### 3.1 Schema Updates

**File**: `src/bug_bot/schemas/admin.py`

**Updated schemas:**
- `TeamCreate` — add `name` (required), `description`, `slack_channel_id`
- `TeamUpdate` — add `name`, `description`, `slack_channel_id`
- `TeamRotationConfigUpdate` — add `rotation_interval`, `handoff_day`, `handoff_time`, `'weighted'` to rotation_type
- `TeamResponse` — add `name`, `slug`, `description`, `slack_channel_id`, `rotation_interval`, `handoff_day`, `handoff_time`, `is_active`
- `OnCallScheduleResponse` — add `origin`
- `OnCallOverrideResponse` — add `status`, `requested_by`, `approved_by`
- Service schemas — add `repository_url`, `environment`, `tier`, `metadata`, `is_active`

**New schemas:**
- `TeamMembershipResponse` — id, team_id, slack_user_id, team_role, is_eligible_for_oncall, weight, joined_at, display_name
- `TeamMembershipUpsert` — slack_user_id, team_role?, is_eligible_for_oncall?, weight?
- `OnCallAuditLogResponse` — id, team_id, entity_type, entity_id, action, actor_type, actor_id, changes, metadata, created_at
- `PaginatedOnCallAuditLogs`
- `OverrideStatusUpdate` — status (approved/rejected/cancelled), approved_by
- `RotationPreviewEntry` — week_number, start_date, end_date, engineer_slack_id
- `RotationPreviewResponse`
- `GlobalOnCallResponse` — engineer_slack_id, team_id, team_name, service_name, source

### 3.2 Update Existing Endpoints

**File**: `src/bug_bot/api/admin.py`

| Endpoint | Changes |
|----------|---------|
| `POST /teams` | Accept `name`, `description`, `slack_channel_id`; auto-generate slug |
| `PATCH /teams/{id}` | Accept `name`, `description`, `slack_channel_id`; audit log |
| `DELETE /teams/{id}` | Soft delete; audit log |
| `GET /teams` | Filter by `is_active` (default true) |
| `PATCH /teams/{id}/rotation-config` | Accept `rotation_interval`, `handoff_day`, `handoff_time`, `weighted`; on change, delete future auto schedules + regenerate |
| `POST /teams/{id}/oncall-overrides` | Set `status` based on config (default `approved`), set `requested_by` |
| Service endpoints | Add `tier` filter, soft delete, return new fields |

### 3.3 New Endpoints

**File**: `src/bug_bot/api/admin.py`

**Team Members:**
```
GET    /teams/{team_id}/members                      → merged Slack + DB list
POST   /teams/{team_id}/members                      → upsert member metadata
PATCH  /teams/{team_id}/members/{slack_user_id}      → update role/weight/eligibility
DELETE /teams/{team_id}/members/{slack_user_id}       → remove metadata
```

**Override Approval:**
```
PATCH  /teams/{team_id}/oncall-overrides/{id}        → approve/reject/cancel
```

**Rotation Preview & Generate:**
```
POST   /teams/{team_id}/rotation/preview             → simulate next N weeks
POST   /teams/{team_id}/rotation/generate            → create auto schedules
```

**Audit Logs:**
```
GET    /audit-logs                                    → filterable (entity_type, action, actor_id, team_id, date range)
```

**Global Lookup:**
```
GET    /oncall?service={name}&team={name}             → who is on-call?
GET    /services/{service_id}/oncall                  → on-call for specific service
GET    /users/{slack_id}/schedules                    → all schedules across teams
```

### Verification
- API tests: all new endpoints CRUD lifecycle
- Backward compatibility: existing API calls without new fields still work
- Override state machine: pending→approved, pending→rejected, approved→cancelled (invalid transitions return 400)
- Rotation preview returns correct sequence for all 3 strategies
- Audit log filtering works by entity_type, action, date range
- Soft delete: deleted items excluded from listings, still accessible by ID

---

## Phase 4: Data Migration & Legacy Cleanup

**Goal**: Migrate `oncall_history` data into `oncall_audit_logs`, stop dual-write, retire old table.

### 4.1 One-Time Migration Script

**New file**: `scripts/migrate_oncall_history_to_audit_logs.py`

Mapping:
| oncall_history.change_type | → action | → entity_type |
|---------------------------|----------|---------------|
| `manual` | `updated` | `team` |
| `auto_rotation` | `rotation_triggered` | `team` |
| `schedule_created` | `created` | `schedule` |
| `schedule_updated` | `updated` | `schedule` |
| `schedule_deleted` | `deleted` | `schedule` |
| `override_created` | `created` | `override` |
| `override_deleted` | `deleted` | `override` |

Set `actor_type='user'` if `changed_by` is set, else `'system'`. Preserve all legacy columns.

Run as standalone script (NOT Alembic migration).

### 4.2 Stop Dual-Writing

**File**: `src/bug_bot/db/repository.py`
- Remove `OnCallHistory` write from `log_oncall_change()` — now only writes to `oncall_audit_logs`

### 4.3 Update History API

**File**: `src/bug_bot/api/admin.py`
- `GET /teams/{id}/oncall-history` reads from `oncall_audit_logs` filtered by `team_id`
- Response shape stays backward-compatible (populate from legacy columns)

### 4.4 (Optional) Drop oncall_history Table

Alembic migration to drop the table after confirming no remaining reads.

### Verification
- Row counts match after migration
- History API returns same data
- No writes to old table
- Audit log query performance acceptable with indexes

---

## Phase 5: Frontend — Types, API Client, Core Updates

**Goal**: Update all FE types, API client, hooks, and existing pages to work with expanded backend.

### 5.1 Update Types

**File**: `firstline-ai/src/types/oncall.ts`
- `TeamResponse` — add `name`, `slug`, `description`, `slack_channel_id`, `rotation_interval`, `handoff_day`, `handoff_time`, `is_active`
- `TeamCreate` — add `name` (required), `description`, `slack_channel_id`
- `TeamUpdate` — add `name`, `description`, `slack_channel_id`
- `RotationConfigUpdate` — add `rotation_interval`, `handoff_day`, `handoff_time`, `'weighted'` type
- `OnCallSchedule` — add `origin: 'auto' | 'manual'`
- `OnCallOverride` — add `status`, `requested_by`, `approved_by`
- New: `TeamMembership`, `TeamMembershipUpsert`, `OnCallAuditLogEntry`, `OverrideStatusUpdate`, `RotationPreviewEntry`

### 5.2 Update API Client

**File**: `firstline-ai/src/api/realClient.ts`

New functions:
- `listTeamMemberships(teamId)`, `upsertTeamMembership()`, `updateTeamMembership()`, `deleteTeamMembership()`
- `updateOverrideStatus(teamId, overrideId, payload)`
- `previewRotation(teamId, weeks)`, `generateRotationSchedules(teamId, weeks)`
- `listAuditLogs(filters)`
- `lookupOnCall({ service?, team? })`
- `getUserSchedules(slackId, fromDate, toDate)`

### 5.3 Update Hooks

**File**: `firstline-ai/src/hooks/useOnCall.ts`

New hooks: `useTeamMemberships`, `useUpsertTeamMembership`, `useUpdateTeamMembership`, `useDeleteTeamMembership`, `useUpdateOverrideStatus`, `useRotationPreview`, `useGenerateRotationSchedules`, `useAuditLogs`, `useGlobalOnCallLookup`

### 5.4 Update Existing Components

| Component | Changes |
|-----------|---------|
| **CreateTeamModal** | Add `name` (required text), `description` (textarea), `slack_channel_id` (select) |
| **RotationConfigCard** | Add `rotation_interval` dropdown (daily/weekly/biweekly), `handoff_day` picker, `handoff_time` input, `'Weighted'` type option, "Preview" button |
| **OnCallListPage** | Display `team.name` instead of Slack group lookup, show `rotation_interval` in badge |
| **OnCallDetailPage** | Add "Members" tab, show `origin` badge on schedules, show `status` + approve/reject buttons on overrides, display `name`/`description` in header |
| **OverrideModal** | No structural change (backend manages status). Update override table to show status column + action buttons |
| **ScheduleModal** | No change needed |

### Verification
- Team list shows name/description, create works with new fields
- Rotation config shows interval/handoff/weighted options
- Schedule table shows origin badges
- Override table shows status badges, approve/reject buttons work
- All existing functionality still works (regression)

---

## Phase 6: Frontend — New Pages & Components

**Goal**: Build new pages for members management, service directory, audit log viewer, global search, and dashboard summary.

### 6.1 Team Members Tab

**New file**: `firstline-ai/src/components/oncall/TeamMembersTab.tsx`
- Table: Name, Role (dropdown), Eligible (toggle), Weight (editable number), Joined
- Inline editing with save
- Members from Slack but not in DB shown with default values
- "Sync from Slack" button

Wire into OnCallDetailPage as new tab between "Schedules" and "Rotation".

### 6.2 Service Management Page

**New file**: `firstline-ai/src/pages/ServiceManagement.tsx`
- Paginated table: Name, Repo URL, Team, Tier, Owner, Tech Stack, Environment
- Filters: team, tier
- Create/Edit modal with all fields
- Soft delete with confirmation
- "Who's on-call?" button per service

**New route**: `/services` in `App.tsx`

### 6.3 Audit Log Viewer

**New file**: `firstline-ai/src/pages/AuditLogViewer.tsx`
- Paginated table: Timestamp, Entity Type, Entity, Action, Actor, Changes
- Filter sidebar: entity_type, action, date range, actor search
- Expandable row showing `changes` diff (old → new values)
- Link entity_id to relevant detail page

**New route**: `/audit-logs` in `App.tsx`

### 6.4 Global On-Call Search

**New file**: `firstline-ai/src/components/oncall/OnCallSearch.tsx`
- Search input: "Who is on-call for..."
- Combobox with service/team suggestions
- Results card: engineer name, team, source, effective dates

Integrate into nav bar or standalone at `/oncall-lookup`.

### 6.5 Dashboard On-Call Summary

**New section** on existing Dashboard page:
- "Active On-Calls" card: all teams with current on-call engineer
- Next rotation timestamps
- Active overrides highlighted

### 6.6 Rotation Preview Panel

**New file**: `firstline-ai/src/components/oncall/RotationPreview.tsx`
- Triggered from RotationConfigCard ("Preview upcoming rotation" button)
- Shows next 4 weeks in timeline/table
- "Generate Schedules" button to persist as auto schedules

### 6.7 Route & Navigation Updates

**File**: `firstline-ai/src/App.tsx`
- Add `/services`, `/audit-logs`, `/oncall-lookup` routes

Update sidebar to include Service Management, Audit Logs, and On-Call Lookup links.

### Verification
- Members tab: edit role/weight/eligibility, save persists, sync works
- Service management: full CRUD, tier filtering, on-call lookup per service
- Audit log: filters work, date range, changes diff renders correctly
- On-call search: by service name and team name returns correct engineer
- Rotation preview: accurate for all 3 strategies, generate creates auto schedules
- **End-to-end**: create team → add members with weights → configure weighted rotation → preview → generate → create override with approval → verify audit trail captures everything

---

## Phase Dependencies

```
Phase 1 (Schema)
  └→ Phase 2 (Backend Logic)
       └→ Phase 3 (API Endpoints)
            ├→ Phase 4 (Data Migration) — can run in parallel with Phase 5
            └→ Phase 5 (FE Core Updates)
                 └→ Phase 6 (FE New Pages)
```

## Key Risk Areas

1. **Weighted rotation correctness** — needs thorough testing; edge cases: equal weights (= round_robin), new member mid-cycle, all members ineligible
2. **Slack group sync timing** — members removed from Slack should be excluded from rotation but DB metadata preserved
3. **Audit log migration** — change_type → action mapping must be exhaustive; validate with production counts
4. **Override approval race conditions** — two admins approving/rejecting simultaneously; solve with optimistic locking (`SELECT ... FOR UPDATE`)
5. **Auto-schedule overlap** — `generate_schedule_lookahead()` must delete future `origin='auto'` schedules before recreating; never touch manual schedules or approved overrides

## Critical Files

| Area | File |
|------|------|
| DB Models | `src/bug_bot/models/models.py` |
| Repository | `src/bug_bot/db/repository.py` |
| API Endpoints | `src/bug_bot/api/admin.py` |
| Pydantic Schemas | `src/bug_bot/schemas/admin.py` |
| Oncall Service | `src/bug_bot/oncall/service.py` |
| Rotation Engine | `src/bug_bot/oncall/rotation.py` |
| Slack Notifications | `src/bug_bot/oncall/slack_notifications.py` |
| Temporal Workflow | `src/bug_bot/temporal/workflows/oncall_rotation.py` |
| Temporal Activities | `src/bug_bot/temporal/activities/database_activity.py` |
| Worker | `src/bug_bot/worker.py` |
| FE Types | `firstline-ai/src/types/oncall.ts` |
| FE API Client | `firstline-ai/src/api/realClient.ts` |
| FE Hooks | `firstline-ai/src/hooks/useOnCall.ts` |
| FE Team List | `firstline-ai/src/pages/OnCallList.tsx` |
| FE Team Detail | `firstline-ai/src/pages/OnCallDetail.tsx` |
| FE Oncall Components | `firstline-ai/src/components/oncall/` |
