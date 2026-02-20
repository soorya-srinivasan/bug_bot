# Plan: Auto-Close Scheduled Job

## Context

Open bugs that receive no interaction from developers or reporters accumulate indefinitely.
This plan adds an hourly Temporal schedule that finds stale open bugs (no human interaction for
a configurable number of days, default 5) and closes them — signalling the live
`BugInvestigationWorkflow` if it is still running, or writing directly to the DB if it is not.
Workspace directories are cleaned up in both paths. No external cron is needed; everything runs
inside Temporal.

---

## High-Level Flow

```
Worker startup
  └─ create_schedule("auto-close-hourly-schedule", every=1h)   ← idempotent

Every hour, Temporal fires AutoCloseWorkflow:
  └─ find_stale_bugs(inactivity_days)   ← DB activity
      For each stale bug:
        ├─ workflow_id present?
        │    ├─ try: signal close_requested on BugInvestigationWorkflow
        │    │         (workflow handles its own DB update + cleanup)
        │    └─ except (workflow dead): fall through ↓
        └─ direct path: mark_bug_auto_closed() + cleanup_workspace()
```

**Staleness definition:** a bug is stale when
`COALESCE(max(human conversation created_at), bug created_at) < NOW() - inactivity_days`.
Human conversations = `BugConversation.sender_type IN ('reporter', 'developer')`.
Excluded statuses: `resolved`, `escalated` (SLA workflow owns those).

---

## Files to Create / Modify

| File | Change |
|------|--------|
| `config.py` | Add `auto_close_inactivity_days: int = 5` |
| `db/repository.py` | Add `get_stale_open_bugs(threshold)` |
| `temporal/activities/database_activity.py` | Add `find_stale_bugs` + `mark_bug_auto_closed` activities |
| `temporal/workflows/auto_closer.py` | **New file** — `AutoCloseWorkflow` |
| `worker.py` | Register new workflow + activities; call `_ensure_auto_close_schedule` at startup |

---

## Detailed Changes

### 1. `src/bug_bot/config.py`

```python
# Auto-close (after temporal_task_queue)
auto_close_inactivity_days: int = 5   # env: AUTO_CLOSE_INACTIVITY_DAYS
```

---

### 2. `src/bug_bot/db/repository.py`

Add after `get_recent_open_bugs`. Uses a correlated scalar subquery; the existing
`idx_bug_conversations_bug_id` index covers the inner lookup.

```python
async def get_stale_open_bugs(self, threshold: datetime) -> list[BugReport]:
    """Open bugs whose last human interaction (or creation date) is before `threshold`.

    Excludes 'resolved' and 'escalated' (SLA workflow owns escalated bugs).
    """
    last_human_sq = (
        select(func.max(BugConversation.created_at))
        .where(
            BugConversation.bug_id == BugReport.bug_id,
            BugConversation.sender_type.in_(["reporter", "developer"]),
        )
        .correlate(BugReport)
        .scalar_subquery()
    )
    stmt = (
        select(BugReport)
        .where(
            BugReport.status.not_in(["resolved", "escalated"]),
            func.coalesce(last_human_sq, BugReport.created_at) < threshold,
        )
        .order_by(BugReport.created_at)
    )
    result = await self.session.execute(stmt)
    return list(result.scalars().all())
```

`BugConversation` is already imported at the top of `repository.py`.

---

### 3. `src/bug_bot/temporal/activities/database_activity.py`

Add at the top of the file:
```python
from datetime import datetime, timedelta
```

Add two new activities at the bottom:

```python
@activity.defn
async def find_stale_bugs(inactivity_days: int) -> list[dict]:
    """Return open bugs with no human interaction in the last inactivity_days days."""
    threshold = datetime.utcnow() - timedelta(days=inactivity_days)
    async with async_session() as session:
        bugs = await BugRepository(session).get_stale_open_bugs(threshold)
    activity.logger.info(f"Found {len(bugs)} stale bugs (threshold={threshold.date()})")
    return [
        {"bug_id": b.bug_id, "temporal_workflow_id": b.temporal_workflow_id, "status": b.status}
        for b in bugs
    ]


@activity.defn
async def mark_bug_auto_closed(bug_id: str) -> None:
    """Resolve a bug directly and log the auto-close event (used when workflow is not running)."""
    async with async_session() as session:
        repo = BugRepository(session)
        await repo.update_status(bug_id, "resolved")
        await repo.log_conversation(
            bug_id=bug_id,
            message_type="resolved",
            sender_type="system",
            sender_id=None,
            channel=None,
            message_text="Auto-closed due to inactivity",
            metadata={"reason": "auto_close_inactivity"},
        )
    activity.logger.info(f"Bug {bug_id} auto-closed (direct path)")
```

---

### 4. `src/bug_bot/temporal/workflows/auto_closer.py` *(new file)*

```python
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from bug_bot.temporal.activities.database_activity import (
        find_stale_bugs,
        mark_bug_auto_closed,
    )
    from bug_bot.temporal.activities.agent_activity import cleanup_workspace
    from bug_bot.temporal.workflows.bug_investigation import BugInvestigationWorkflow


@dataclass
class AutoCloseInput:
    inactivity_days: int = 5


@workflow.defn
class AutoCloseWorkflow:
    """Hourly scheduled workflow — closes stale open bugs."""

    @workflow.run
    async def run(self, input: AutoCloseInput) -> dict:
        stale: list[dict] = await workflow.execute_activity(
            find_stale_bugs,
            args=[input.inactivity_days],
            start_to_close_timeout=timedelta(seconds=30),
        )

        if not stale:
            workflow.logger.info("No stale bugs found")
            return {"closed": 0, "signaled": 0, "direct": 0, "errors": 0}

        workflow.logger.info(f"Auto-closing {len(stale)} stale bugs")
        signaled = direct = errors = 0

        for bug in stale:
            bug_id: str = bug["bug_id"]
            workflow_id: str | None = bug["temporal_workflow_id"]
            try:
                closed_via_signal = False
                if workflow_id:
                    try:
                        handle = workflow.get_external_workflow_handle_for(
                            BugInvestigationWorkflow.run, workflow_id
                        )
                        await handle.signal(BugInvestigationWorkflow.close_requested)
                        signaled += 1
                        closed_via_signal = True
                        workflow.logger.info(f"Signalled close for {bug_id}")
                    except Exception as e:
                        # Workflow already finished or was never running
                        workflow.logger.warning(
                            f"Signal failed for {bug_id} ({type(e).__name__}), using direct path"
                        )

                if not closed_via_signal:
                    await workflow.execute_activity(
                        mark_bug_auto_closed,
                        args=[bug_id],
                        start_to_close_timeout=timedelta(seconds=15),
                    )
                    await workflow.execute_activity(
                        cleanup_workspace,
                        args=[bug_id],
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                    direct += 1
                    workflow.logger.info(f"Directly closed {bug_id}")

            except Exception as e:
                workflow.logger.error(f"Failed to close {bug_id}: {e}")
                errors += 1

        return {"closed": signaled + direct, "signaled": signaled, "direct": direct, "errors": errors}
```

**Signal path**: `BugInvestigationWorkflow._handle_close()` takes care of its own DB update,
conversation log, and `cleanup_workspace` — so the `AutoCloseWorkflow` does NOT call those
redundantly for the signal path.

**Direct path**: used when `temporal_workflow_id` is null, or when the signal raises (workflow
already ended). `cleanup_workspace` silently no-ops if the directory doesn't exist
(`shutil.rmtree(..., ignore_errors=True)`).

---

### 5. `src/bug_bot/worker.py`

**New imports:**
```python
from datetime import timedelta
from temporalio.client import (
    Schedule, ScheduleActionStartWorkflow, ScheduleSpec,
    ScheduleIntervalSpec, SchedulePolicy, ScheduleOverlapPolicy,
)
from bug_bot.temporal.workflows.auto_closer import AutoCloseWorkflow, AutoCloseInput
from bug_bot.temporal.activities.database_activity import (
    ...,          # existing
    find_stale_bugs,
    mark_bug_auto_closed,
)
```

**New helper** (before `main`):
```python
SCHEDULE_ID = "auto-close-hourly-schedule"

async def _ensure_auto_close_schedule(client: Client) -> None:
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            AutoCloseWorkflow.run,
            AutoCloseInput(inactivity_days=settings.auto_close_inactivity_days),
            id="auto-close-hourly",
            task_queue=settings.temporal_task_queue,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(hours=1))]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )
    try:
        await client.create_schedule(SCHEDULE_ID, schedule)
        logging.info("Auto-close schedule created")
    except Exception as e:
        # RPCError ALREADY_EXISTS — schedule already registered, nothing to do
        if "already" in str(e).lower():
            logging.info("Auto-close schedule already exists, skipping")
        else:
            raise
```

**In `main()`, before `Worker(...)` construction:**
```python
await _ensure_auto_close_schedule(client)
```

**Add to `workflows` list:** `AutoCloseWorkflow`

**Add to `activities` list:** `find_stale_bugs`, `mark_bug_auto_closed`

---

## Reused Existing Code

| Reused | Location |
|--------|----------|
| `cleanup_workspace` activity | `temporal/activities/agent_activity.py` |
| `BugInvestigationWorkflow.close_requested` signal | `temporal/workflows/bug_investigation.py:70` |
| `BugRepository.update_status` | `db/repository.py:56` |
| `BugRepository.log_conversation` | `db/repository.py:449` |
| `async_session` | `db/session.py` |
| `@activity.defn` pattern | Throughout `temporal/activities/` |

---

## Verification

1. **Unit-check the query**: In a psql shell, confirm `get_stale_open_bugs` returns the expected
   rows by inserting a test bug with a `created_at` older than 5 days and no conversations.

2. **Trigger manually via Temporal UI**: After worker restart, use the Temporal UI to find
   the `auto-close-hourly-schedule` schedule and click "Trigger Now". Watch the
   `AutoCloseWorkflow` run appear and complete.

3. **End-to-end**: Create a bug, let it sit without any Slack replies, set
   `AUTO_CLOSE_INACTIVITY_DAYS=0` temporarily, restart the worker, and trigger the schedule.
   Verify `bug_reports.status = 'resolved'` and a `resolved` row in `bug_conversations`.

4. **Active workflow path**: Create a bug that has a running `BugInvestigationWorkflow` stuck
   in `AWAITING_DEV`, trigger the schedule, verify the workflow receives `close_requested` and
   terminates cleanly.

5. **Idempotency**: Restart the worker twice; confirm only one schedule exists in Temporal UI.
