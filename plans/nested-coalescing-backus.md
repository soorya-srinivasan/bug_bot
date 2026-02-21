# Oncall Management Improvements

## Context

The oncall management system spans two repos: **bug_bot** (FastAPI backend) and **firstline-ai** (React frontend). While the core schedule/rotation/manual-assignment flow works, there are five gaps identified:

1. Manual oncall updates (`PATCH /teams/{id}`) don't create audit history entries
2. No mechanism to temporarily override the scheduled oncall for specific dates (e.g., sick day swap)
3. Schedule type labels ("Weekly"/"Daily") are confusing — "Weekly" means continuous coverage, "Daily" means specific weekdays
4. Schedule creation uses raw Slack ID text input instead of a team member dropdown
5. `created_by` is hardcoded as `"ADMIN"` (accepted for now — deferred to a future iteration)

**Decisions made:**
- Keep the existing permanent manual fallback (`team.oncall_engineer`) AND add a separate date-specific override feature
- Fix schedule type labels in frontend only (no backend migration)
- Keep `created_by` as `"ADMIN"` for now

---

## Changes

### 1. Add audit logging for manual oncall updates (Gap 1)

**Files:**
- `bug_bot/src/bug_bot/api/admin.py` — `update_team` endpoint (line 863)

**What:** Before calling `repo.update_team()`, fetch the current team to capture `old_oncall_engineer`. After update, if `oncall_engineer` changed, call `repo.log_oncall_change()` with `change_type="manual"`.

```python
@router.patch("/teams/{id}", response_model=TeamResponse)
async def update_team(id: str, payload: TeamUpdate, repo = Depends(get_repo)):
    old_team = await repo.get_team_by_id(id)
    if old_team is None:
        raise HTTPException(404, "Team not found")
    old_oncall = old_team.oncall_engineer

    data = {k: v for k, v in payload.model_dump().items() if v is not None}
    t = await repo.update_team(id, data)
    if t is None:
        raise HTTPException(404, "Team not found")

    # Log history if oncall_engineer changed
    if "oncall_engineer" in data and data["oncall_engineer"] != old_oncall:
        await repo.log_oncall_change(
            team_id=id,
            engineer_slack_id=data["oncall_engineer"],
            change_type="manual",
            effective_date=date.today(),
            previous_engineer_slack_id=old_oncall,
            change_reason="Manual assignment via admin panel",
        )

    return TeamResponse(...)
```

**File:** `firstline-ai/src/hooks/useOnCall.ts` — In `useUpdateTeam`, add `["oncall-history", teamId]` to the cache invalidation list so the History tab refreshes after manual assignment.

---

### 2. Add date-specific override mechanism (Gap 2)

The override sits at the **top** of the resolution chain:
`Override → Schedule → Rotation → Manual (team.oncall_engineer) → ServiceTeamMapping.primary_oncall`

#### 2a. New database table + model

**New migration file:** `bug_bot/alembic/versions/xxxx_add_oncall_overrides.py`

```sql
CREATE TABLE oncall_overrides (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    override_date DATE NOT NULL,
    end_date DATE,                          -- NULL = single day
    substitute_engineer_slack_id VARCHAR(20) NOT NULL,
    original_engineer_slack_id VARCHAR(20),
    reason TEXT NOT NULL,
    created_by VARCHAR(20) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_oncall_overrides_team_date ON oncall_overrides(team_id, override_date);
```

**File:** `bug_bot/src/bug_bot/models/models.py`
- Add `OnCallOverride` model class after `OnCallSchedule` (line ~270)
- Add `overrides` relationship to `Team` model (line ~123)

#### 2b. Repository methods

**File:** `bug_bot/src/bug_bot/db/repository.py`

Add methods:
- `get_active_override_for_team(team_id, check_date)` — query override where `override_date <= check_date` and (`end_date IS NULL AND override_date == check_date` OR `end_date >= check_date`), ordered by `created_at DESC`, limit 1
- `create_oncall_override(team_id, data)` — insert and return
- `list_oncall_overrides(team_id, page, page_size)` — paginated list, ordered by `override_date DESC`
- `delete_oncall_override(id_)` — delete by ID
- `check_override_overlap(team_id, override_date, end_date, exclude_id)` — prevent conflicting overrides for same team/dates

**Modify** `get_current_oncall_for_team()` (line 793) — add override check as the **first** step before the existing schedule check:

```python
# 1. Check for active override (highest priority)
override = await self.get_active_override_for_team(team_id, check_date)
if override:
    return {
        "engineer_slack_id": override.substitute_engineer_slack_id,
        "effective_date": override.override_date,
        "source": "override",
        "schedule_id": None,
    }

# 2. Existing schedule check (unchanged)...
# 3. Existing manual fallback (unchanged)...
```

#### 2c. Service layer

**File:** `bug_bot/src/bug_bot/oncall/service.py`
- In `get_current_oncall()` (line 92), update the early return condition to recognize `"override"` as a valid source:
  ```python
  if current and current.get("source") in ("schedule", "override"):
      return current
  ```

#### 2d. Schemas

**File:** `bug_bot/src/bug_bot/schemas/admin.py`

Add:
```python
class OnCallOverrideCreate(BaseModel):
    override_date: date
    end_date: date | None = None
    substitute_engineer_slack_id: str
    original_engineer_slack_id: str | None = None
    reason: str

class OnCallOverrideResponse(BaseModel):
    id: str
    team_id: str
    override_date: date
    end_date: date | None
    substitute_engineer_slack_id: str
    original_engineer_slack_id: str | None
    reason: str
    created_by: str
    created_at: datetime

class PaginatedOnCallOverrides(BaseModel):
    items: list[OnCallOverrideResponse]
    total: NonNegativeInt
    page: int
    page_size: int
```

Update existing schemas:
- `CurrentOnCallResponse.source` → add `"override"` to the Literal
- `OnCallHistoryResponse.change_type` → add `"override_created"`, `"override_deleted"` to the Literal

#### 2e. API endpoints

**File:** `bug_bot/src/bug_bot/api/admin.py`

Add three endpoints:
- `POST /teams/{team_id}/oncall-overrides` — create override, log to history with `change_type="override_created"`
- `GET /teams/{team_id}/oncall-overrides` — list overrides (paginated)
- `DELETE /teams/{team_id}/oncall-overrides/{override_id}` — delete override, log to history with `change_type="override_deleted"`

The create endpoint should:
1. Validate team exists
2. Check for overlapping overrides (same team, overlapping dates)
3. Auto-populate `original_engineer_slack_id` from current oncall if not provided
4. Create the override record
5. Log to `OnCallHistory`

#### 2f. Frontend types

**File:** `firstline-ai/src/types/oncall.ts`

Add `OnCallOverride`, `OnCallOverrideCreate` interfaces.
Update `CurrentOnCall.source` to include `"override"`.
Update `ChangeType` to include `"override_created"` and `"override_deleted"`.

#### 2g. Frontend API client

**File:** `firstline-ai/src/api/realClient.ts`

Add: `createOnCallOverride()`, `listOnCallOverrides()`, `deleteOnCallOverride()`

**File:** `firstline-ai/src/api/mockClient.ts` — add corresponding mock implementations.

#### 2h. Frontend hooks

**File:** `firstline-ai/src/hooks/useOnCall.ts`

Add: `useOnCallOverrides(teamId)`, `useCreateOnCallOverride(teamId)`, `useDeleteOnCallOverride(teamId)`

Mutations invalidate: `["current-oncall", teamId]`, `["oncall-overrides", teamId]`, `["oncall-history", teamId]`

#### 2i. Override Modal component

**New file:** `firstline-ai/src/components/oncall/OverrideModal.tsx`

Fields:
- **Substitute engineer** — `AppSelect` dropdown from team members (same pattern as manual assignment in OnCallDetail)
- **Override date** — date input (required)
- **End date** — optional date input (for multi-day overrides)
- **Reason** — text input (required)
- **Original engineer** — auto-populated read-only display from current oncall

#### 2j. OnCallDetail page updates

**File:** `firstline-ai/src/pages/OnCallDetail.tsx`

- Add "Create Override" button in the Current On-Call card (alongside existing "Update On-Call")
- When `currentOnCall.source === "override"`, show the override reason and a "Remove Override" action
- Add a 4th tab **"Overrides"** (alongside Schedules / Rotation / History) showing active/upcoming overrides in a table with delete buttons
- Add `"override"` badge styling (amber color) in the source badge renderer
- Add `override_created` / `override_deleted` to `CHANGE_TYPE_STYLES` (amber)

**File:** `firstline-ai/src/pages/OnCallList.tsx` — add `"override"` source badge styling in the team list table.

---

### 3. Fix schedule type labels (Gap 3)

Frontend-only changes — backend values `"weekly"` / `"daily"` remain unchanged.

**File:** `firstline-ai/src/components/oncall/ScheduleModal.tsx` (line 95-98)

Change labels:
- `"Weekly (full period)"` → `"Continuous (every day in range)"`
- `"Daily (specific days)"` → `"Selected days only"`

**File:** `firstline-ai/src/pages/OnCallDetail.tsx` (line 288)

Map schedule type display values:
- `"weekly"` → `"Continuous"`
- `"daily"` → `"Selected Days"`

---

### 4. Replace raw Slack ID input with team member dropdown (Gap 4)

**File:** `firstline-ai/src/components/oncall/ScheduleModal.tsx`

- Add `teamId: string` to `ScheduleModalProps`
- Import and use `useTeamMembers(teamId)` hook
- Replace `FormInput` for engineer (line 70-76) with `AppSelect` dropdown showing non-bot, non-deleted team members (same pattern as manual assignment in OnCallDetail.tsx:213-226)

**File:** `firstline-ai/src/pages/OnCallDetail.tsx` (line 367-374)

- Pass `teamId={team_id!}` prop to `<ScheduleModal>`

---

## Implementation Order

| Phase | Work | Files |
|-------|------|-------|
| 1 | DB migration for `oncall_overrides` table | `alembic/versions/xxxx_...py` |
| 2 | Backend model + repository + service | `models.py`, `repository.py`, `service.py` |
| 3 | Backend schemas + API endpoints + manual update audit fix | `admin.py`, `schemas/admin.py` |
| 4 | Frontend types + API client + hooks | `oncall.ts`, `realClient.ts`, `mockClient.ts`, `useOnCall.ts` |
| 5 | Frontend UI: OverrideModal, OnCallDetail updates, ScheduleModal fixes | `OverrideModal.tsx`, `OnCallDetail.tsx`, `ScheduleModal.tsx`, `OnCallList.tsx` |

---

## Verification

1. **Manual assignment audit**: Change a team's oncall engineer via "Update On-Call" → verify a new entry appears in History tab with `change_type="manual"`
2. **Override creation**: Create an override for today → verify `current-oncall` API returns `source="override"` with the substitute engineer → verify History tab shows `override_created` entry
3. **Override priority**: With both an active schedule AND an active override → verify the override takes priority
4. **Override deletion**: Delete the override → verify oncall falls back to the schedule
5. **Override for date range**: Create a multi-day override → verify it applies on all days in range and not outside it
6. **Schedule labels**: Open schedule modal → verify labels read "Continuous (every day in range)" and "Selected days only"
7. **Engineer dropdown**: Open schedule modal → verify engineer selection is a dropdown of team members, not a text input
8. **Bug view**: View a bug → verify `tagged_on` (oncall at report time) and `current_on_call` (today, respecting overrides) both display correctly
