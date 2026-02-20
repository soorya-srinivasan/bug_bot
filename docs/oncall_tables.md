# On-Call Management System — Table Roles and Responsibilities

This document describes the role and responsibility of each database table used by the on-call management system, including the new tables and the columns added to existing tables.

---

## 1. `teams` (formerly `service_groups`, extended for on-call)

**Role:** Represents a team of services that share a single on-call rotation. The team is tied to a Slack user group; the actual on-call engineer can come from a schedule, from rotation, or from a manual override.

**Responsibility:**
- Store the Slack user group ID that identifies the team (e.g. `@payments-oncall`).
- Hold the **current** on-call engineer (Slack user ID) when not overridden by an active schedule.
- Store rotation configuration so the system can auto-rotate or manually rotate on-call.

**Columns relevant to on-call:**

| Column | Type | Responsibility |
|--------|------|----------------|
| `id` | UUID | Primary key. |
| `slack_group_id` | String(30) | Slack user group ID; source of identity and (for round_robin) list of engineers. |
| `oncall_engineer` | String(20) nullable | Slack user ID of current on-call when no active schedule applies. Fallback after schedule/rotation. |
| `rotation_enabled` | Boolean | Whether automatic or manual rotation is used for this team. |
| `rotation_type` | String(20) nullable | `round_robin` (use Slack group members) or `custom_order` (use `rotation_order`). |
| `rotation_order` | JSONB nullable | For `custom_order`: ordered list of Slack user IDs. |
| `rotation_start_date` | Date nullable | Date from which rotation is considered active (e.g. start of first rotation week). |
| `current_rotation_index` | Integer nullable | Index in `rotation_order` (or in Slack group list for round_robin) of current on-call. |
| `created_at`, `updated_at` | Timestamp | Audit timestamps. |

**Relationships:**
- One-to-many with `service_team_mapping` (services in the team; FK `team_id`).
- One-to-many with `oncall_schedules` (future/current schedule entries; FK `team_id`).
- One-to-many with `oncall_history` (audit log of on-call changes; FK `team_id`).

**Example row (manual on-call, no rotation):**

| id | slack_group_id | oncall_engineer | rotation_enabled | rotation_type | rotation_order | rotation_start_date | current_rotation_index |
|----|----------------|-----------------|------------------|---------------|----------------|---------------------|------------------------|
| a1b2... | S0PAYMENTS | U_ALICE | false | null | null | null | null |

**Example row (round-robin rotation):**

| id | slack_group_id | oncall_engineer | rotation_enabled | rotation_type | rotation_order | rotation_start_date | current_rotation_index |
|----|----------------|-----------------|------------------|---------------|----------------|---------------------|------------------------|
| a1b2... | S0PAYMENTS | U_BOB | true | round_robin | null | 2026-02-24 | 1 |

**Example row (custom order rotation):**

| id | slack_group_id | oncall_engineer | rotation_enabled | rotation_type | rotation_order | rotation_start_date | current_rotation_index |
|----|----------------|-----------------|------------------|---------------|----------------|---------------------|------------------------|
| a1b2... | S0AUTH | U_CAROL | true | custom_order | ["U_ALICE","U_BOB","U_CAROL"] | 2026-02-24 | 2 |

---

## 2. `oncall_schedules`

**Role:** Stores **who is on-call for which period** for a team. Used to map engineers to upcoming weeks (or specific days) and to override the team’s default `oncall_engineer` when a schedule is active.

**Responsibility:**
- Define time-bounded on-call assignments (weekly or daily).
- Support “map on-call engineers for upcoming weeks” by inserting rows for future date ranges.
- When resolving “current on-call”, the system checks active schedules first, then falls back to `teams.oncall_engineer` and rotation.

**Columns:**

| Column | Type | Responsibility |
|--------|------|----------------|
| `id` | UUID | Primary key. |
| `team_id` | UUID | FK to `teams.id` (CASCADE on delete). Which team this schedule belongs to. |
| `engineer_slack_id` | String(20) | Slack user ID of the engineer on-call for this period. |
| `start_date` | Date | First day (inclusive) of the assignment. |
| `end_date` | Date | Last day (inclusive) of the assignment. |
| `schedule_type` | String(10) | `weekly` = full week; `daily` = only certain days in the range (see `days_of_week`). |
| `days_of_week` | JSONB nullable | For `daily`: array of weekday numbers 0–6 (0 = Monday). e.g. `[0,1,2,3,4]` = weekdays only. |
| `created_by` | String(20) | Slack user ID of the admin who created the schedule. |
| `created_at`, `updated_at` | Timestamp | When the record was created and last updated. |

**Relationships:**
- Many-to-one to `teams` (each schedule belongs to one team).

**Business rules:**
- Overlapping date ranges for the same `team_id` are not allowed (enforced in application logic).
- For `schedule_type = 'daily'`, “current on-call” only applies on days present in `days_of_week`.

**Example — weekly schedule (Alice on-call for one week):**

| id | team_id | engineer_slack_id | start_date | end_date | schedule_type | days_of_week | created_by |
|----|---------|-------------------|------------|----------|---------------|--------------|------------|
| s1... | a1b2... | U_ALICE | 2026-02-24 | 2026-03-02 | weekly | null | U_ADMIN |

**Example — daily schedule (Bob weekdays only for two weeks):**

| id | team_id | engineer_slack_id | start_date | end_date | schedule_type | days_of_week | created_by |
|----|---------|-------------------|------------|----------|---------------|--------------|------------|
| s2... | a1b2... | U_BOB | 2026-03-03 | 2026-03-16 | daily | [0,1,2,3,4] | U_ADMIN |

**Example — mapping next three weeks:**

| engineer_slack_id | start_date | end_date | schedule_type |
|------------------|------------|----------|---------------|
| U_ALICE | 2026-02-24 | 2026-03-02 | weekly |
| U_BOB | 2026-03-03 | 2026-03-09 | weekly |
| U_CAROL | 2026-03-10 | 2026-03-16 | weekly |

---

## 3. `oncall_history`

**Role:** Audit log of **every change** to who is on-call for a team. Supports “on-call log history” and accountability (who changed what, when, and why).

**Responsibility:**
- Record each assignment change: who became on-call, who was on-call before, when it became effective, and how it happened (manual, schedule, rotation).
- Support filtering and reporting by team and time (indexes on `team_id`, `effective_date`, `created_at`).

**Columns:**

| Column | Type | Responsibility |
|--------|------|----------------|
| `id` | UUID | Primary key. |
| `team_id` | UUID | FK to `teams.id` (CASCADE on delete). Which team this change applies to. |
| `engineer_slack_id` | String(20) | Slack user ID of the engineer **now** on-call after this change. |
| `previous_engineer_slack_id` | String(20) nullable | Slack user ID of the previous on-call (null if first assignment). |
| `change_type` | String(20) | How the change happened: `manual`, `auto_rotation`, `schedule_created`, `schedule_updated`, `schedule_deleted`. |
| `change_reason` | Text nullable | Human or system reason (e.g. “Schedule created: weekly from 2026-02-24 to 2026-03-02”, “Automatic rotation (round_robin)”). |
| `effective_date` | Date | Date from which this assignment is effective. |
| `changed_by` | String(20) nullable | Slack user ID of the person who made the change (null for system/auto-rotation). |
| `created_at` | Timestamp | When the history entry was written. |

**Relationships:**
- Many-to-one to `teams` (each history row belongs to one team).

**Example — schedule created:**

| id | team_id | engineer_slack_id | previous_engineer_slack_id | change_type | change_reason | effective_date | changed_by |
|----|---------|-------------------|---------------------------|-------------|---------------|---------------|------------|
| h1... | a1b2... | U_ALICE | U_BOB | schedule_created | Schedule created: weekly from 2026-02-24 to 2026-03-02 | 2026-02-24 | U_ADMIN |

**Example — automatic rotation:**

| id | team_id | engineer_slack_id | previous_engineer_slack_id | change_type | change_reason | effective_date | changed_by |
|----|---------|-------------------|---------------------------|-------------|---------------|---------------|------------|
| h2... | a1b2... | U_CAROL | U_BOB | auto_rotation | Automatic rotation (round_robin) | 2026-03-03 | null |

**Example — manual edit of team on-call:**

| id | team_id | engineer_slack_id | previous_engineer_slack_id | change_type | change_reason | effective_date | changed_by |
|----|---------|-------------------|---------------------------|-------------|---------------|---------------|------------|
| h3... | a1b2... | U_DAVE | U_CAROL | manual | Override for vacation | 2026-03-10 | U_ADMIN |

---

## How the tables work together

1. **Resolving “current on-call” for a team**
   - Look for an active row in `oncall_schedules` (today between `start_date` and `end_date`; for `daily`, today’s weekday in `days_of_week`).
   - If found, that row’s `engineer_slack_id` is the current on-call.
   - If not found and the team has rotation enabled, apply rotation logic using `teams` (and optionally Slack group membership for round_robin).
   - Otherwise use `teams.oncall_engineer`.
   - Any change (schedule create/update/delete, rotation, manual edit) is recorded in `oncall_history`.

2. **Mapping on-call for upcoming weeks**
   - Insert or update rows in `oncall_schedules` for future `start_date`/`end_date` and the desired `engineer_slack_id` per team. Use `schedule_type` and `days_of_week` for weekly vs daily coverage.

3. **On-call log history**
   - Query `oncall_history` by `team_id`, optionally filtering by `effective_date` or `created_at`, to get a full audit trail of who was assigned when and why.

4. **Slack tagging (who gets @-mentioned)**
   - When posting summaries or escalating, the bot tags in this order: **on-call engineer** → **service owner** → **Slack group**. Only one is chosen per team/entry.
   - For the **person** (on-call engineer) to be tagged instead of only the group, at least one of these must be set in the DB:
     - An **active row in `oncall_schedules`** for today (for that team), or
     - **`teams.oncall_engineer`** (Slack user ID), or
     - **`service_team_mapping.primary_oncall`** (Slack user ID) for that service.
   - If all of these are missing for a team, the entry will have only `slack_group_id` and the bot will tag the **Slack group** only. Populate one of the above to get the engineer @-mentioned.

This file focuses only on the tables and columns that are part of the on-call management system and their roles, responsibilities, and examples.
