# Plan: Scheduled Temporal Workflow for On-Call Rotation

## Context

On-call rotation (`should_rotate()` / `process_auto_rotation()`) only fires lazily — when someone queries "who's on call?" or manually hits `POST /teams/{team_id}/rotate`. If no one asks, rotation never happens, engineers don't get notified, and `Team.oncall_engineer` stays stale.

We need a scheduled Temporal workflow that runs daily, finds all teams with rotation enabled, and applies pending rotations proactively — same pattern as the existing `AutoCloseWorkflow`.

## Files to Change

| File | Change |
|------|--------|
| `src/bug_bot/db/repository.py` | Add `get_rotation_enabled_teams()` |
| `src/bug_bot/temporal/activities/database_activity.py` | Add `fetch_rotation_enabled_teams` and `process_team_rotation` activities |
| `src/bug_bot/temporal/workflows/oncall_rotation.py` | **New file** — `OnCallRotationWorkflow` |
| `src/bug_bot/worker.py` | Register workflow + activities, add daily schedule |

No config changes — daily interval hardcoded like the auto-close hourly interval.

## Implementation

### 1. Repository — `get_rotation_enabled_teams()`

Add to `BugRepository` (near existing rotation methods around line 860):

```python
async def get_rotation_enabled_teams(self) -> list[Team]:
    stmt = select(Team).where(Team.rotation_enabled == True)
    result = await self.session.execute(stmt)
    return list(result.scalars().all())
```

### 2. Activities

Add to `database_activity.py`:

**`fetch_rotation_enabled_teams`** — returns lightweight dicts for all rotation-enabled teams:
```python
@activity.defn
async def fetch_rotation_enabled_teams() -> list[dict]:
    async with async_session() as session:
        teams = await BugRepository(session).get_rotation_enabled_teams()
    return [{"id": str(t.id), "slack_group_id": t.slack_group_id} for t in teams]
```

**`process_team_rotation`** — wraps `oncall_service.process_auto_rotation()`, catches exceptions so one team failure doesn't block others:
```python
@activity.defn
async def process_team_rotation(team_id: str) -> dict:
    try:
        async with async_session() as session:
            repo = BugRepository(session)
            rotated = await oncall_service.process_auto_rotation(repo, team_id)
        return {"team_id": team_id, "rotated": rotated}
    except Exception as e:
        activity.logger.error(f"Rotation failed for team {team_id}: {e}")
        return {"team_id": team_id, "rotated": False, "error": str(e)}
```

### 3. Workflow — `OnCallRotationWorkflow`

New file `src/bug_bot/temporal/workflows/oncall_rotation.py`, following `AutoCloseWorkflow` pattern exactly:

```python
@workflow.defn
class OnCallRotationWorkflow:
    @workflow.run
    async def run(self) -> dict:
        teams = await workflow.execute_activity(
            fetch_rotation_enabled_teams,
            start_to_close_timeout=timedelta(seconds=30),
        )
        rotated = skipped = errors = 0
        for team in teams:
            result = await workflow.execute_activity(
                process_team_rotation,
                args=[team["id"]],
                start_to_close_timeout=timedelta(seconds=30),
            )
            if result.get("error"):
                errors += 1
            elif result["rotated"]:
                rotated += 1
            else:
                skipped += 1
        return {"teams": len(teams), "rotated": rotated, "skipped": skipped, "errors": errors}
```

No input dataclass needed — it processes all rotation-enabled teams unconditionally.

### 4. Worker Registration

In `worker.py`:
- Import `OnCallRotationWorkflow`, `fetch_rotation_enabled_teams`, `process_team_rotation`
- Add `OnCallRotationWorkflow` to `workflows=[...]`
- Add both activities to `activities=[...]`
- Add `_ensure_oncall_rotation_schedule()`:
  ```python
  ONCALL_ROTATION_SCHEDULE_ID = "oncall-rotation-daily-schedule"

  async def _ensure_oncall_rotation_schedule(client: Client) -> None:
      schedule = Schedule(
          action=ScheduleActionStartWorkflow(
              OnCallRotationWorkflow.run,
              id="oncall-rotation-daily",
              task_queue=settings.temporal_task_queue,
          ),
          spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(hours=24))]),
          policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
      )
      # same try/except pattern as _ensure_auto_close_schedule
  ```
- Call in `main()` alongside the existing schedule setup

## What's NOT Changed

- `oncall/service.py` — `process_auto_rotation()` reused as-is
- `oncall/rotation.py` — rotation logic untouched
- Lazy rotation in `get_current_oncall()` — still works as fallback
- API endpoints — no changes

## Verification

1. Start the worker → check logs for "On-call rotation schedule created"
2. Create a team with `rotation_enabled=true`, `rotation_type=custom_order`, `rotation_order=[U1, U2]`, `rotation_start_date` = last week
3. Wait for the scheduled workflow to fire (or trigger manually via Temporal UI)
4. Verify `Team.oncall_engineer` updated, `OnCallHistory` has a new `auto_rotation` entry
5. Verify Slack notification sent to the new on-call engineer
