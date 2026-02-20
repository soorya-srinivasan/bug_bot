# Plan: Dev Takeover Flow

## Context

When a developer wants to personally handle a bug instead of relying on the bot's automated
investigation loop, they should be able to signal this from the `#bug-summaries` thread. The
workflow should pause all further Claude SDK invocations, record the dev as the assignee, and
stand by in a dormant wait — only listening for a close signal. Conversations are still logged
normally throughout. The workflow is only closed when explicitly requested.

Additionally, the close path needs a guard: if a close signal arrives after the workflow has
already terminated (e.g. crashed and did not restart), the handler should fall back to updating
the DB and logging the event directly rather than trying to signal a dead workflow.

---

## Changes Required

### 1. `src/bug_bot/models/models.py`
Add `assignee_user_id` to `BugReport`:
```python
assignee_user_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
```
This stores the Slack user ID of the dev who took over. Null means bot-managed.

### 2. `src/bug_bot/db/repository.py`
Add new method to `BugRepository`:
```python
async def update_assignee(self, bug_id: str, user_id: str) -> None:
    stmt = update(BugReport).where(BugReport.bug_id == bug_id).values(
        assignee_user_id=user_id, updated_at=datetime.utcnow()
    )
    await self.session.execute(stmt)
    await self.session.commit()
```

### 3. `src/bug_bot/temporal/__init__.py`
Add new state to `WorkflowState`:
```python
DEV_TAKEOVER = "dev_takeover"
```

### 4. `src/bug_bot/temporal/activities/database_activity.py`
Add new activity:
```python
@activity.defn
async def update_bug_assignee(bug_id: str, user_id: str) -> None:
    async with async_session() as session:
        await BugRepository(session).update_assignee(bug_id, user_id)
    activity.logger.info(f"Assignee for {bug_id} set to {user_id}")
```
Register this in `worker.py` alongside the other activities.

### 5. `src/bug_bot/temporal/workflows/bug_investigation.py`

**Add to imports:**
```python
from bug_bot.temporal.activities.database_activity import (
    ...,
    update_bug_assignee,
)
```

**Add to `__init__`:**
```python
self._dev_takeover: bool = False
self._takeover_user_id: str | None = None
```

**Add new signal handler:**
```python
@workflow.signal
async def dev_takeover(self, dev_user_id: str) -> None:
    """Dev claimed ownership of the bug; stop further Claude investigations."""
    self._dev_takeover = True
    self._takeover_user_id = dev_user_id
```

**Modify `_run` main loop logic:**

If `_dev_takeover` is set while a Claude activity is still running, let that activity complete
naturally. After it returns, post results to #bug-summaries (existing path) so the dev has full
context, then check the flag and enter dormant mode instead of looping back to Claude.

The check happens at the **top of the `while True` loop**, after action processing and after
results are posted — not mid-activity. This means:
1. In-progress Claude run finishes
2. Results are saved + posted to #bug-summaries as normal
3. Loop re-evaluates → sees `_dev_takeover = True` → enters `_handle_dev_takeover`

```python
# TOP of while True, before existing close check:
if self._dev_takeover:
    await self._handle_dev_takeover(input)
    return {"fix_type": "dev_takeover", "bug_id": input.bug_id,
            "assignee": self._takeover_user_id}
```

Add `_handle_dev_takeover` helper:
```python
async def _handle_dev_takeover(self, input: BugReportInput) -> None:
    # Record assignee
    await workflow.execute_activity(
        update_bug_assignee, args=[input.bug_id, self._takeover_user_id],
        start_to_close_timeout=timedelta(seconds=10),
    )
    await workflow.execute_activity(
        update_bug_status, args=[input.bug_id, WorkflowState.DEV_TAKEOVER.value],
        start_to_close_timeout=timedelta(seconds=10),
    )
    await workflow.execute_activity(
        log_conversation_event,
        args=[input.bug_id, "dev_takeover", "developer", self._takeover_user_id,
              None, f"Dev takeover by {self._takeover_user_id}", None],
        start_to_close_timeout=timedelta(seconds=10),
    )
    # Wait indefinitely for a close signal; incoming dev messages are still
    # logged via the existing incoming_message signal → DB path, but we don't
    # feed them to Claude. Use a long timeout (7 days) as a safety net.
    self._state = WorkflowState.DEV_TAKEOVER
    await workflow.wait_condition(
        lambda: self._close_requested,
        timeout=timedelta(days=7),
    )
    if self._close_requested:
        await self._handle_close(input)
    else:
        # 7-day safety-net timeout — auto-resolve
        self._workspace_cleaned = True
        await workflow.execute_activity(
            update_bug_status, args=[input.bug_id, "resolved"],
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            cleanup_workspace, args=[input.bug_id],
            start_to_close_timeout=timedelta(seconds=30),
        )
```

Check at top of `while True` loop (before existing `if self._close_requested`):
```python
if self._dev_takeover:
    await self._handle_dev_takeover(input)
    return {"fix_type": "dev_takeover", "bug_id": input.bug_id}
```

Also add check inside `AWAITING_DEV` `wait_condition` to wake when takeover arrives:
```python
dev_arrived = await workflow.wait_condition(
    lambda: self._close_requested or self._dev_takeover or len(self._message_queue) > 0,
    timeout=timedelta(hours=48),
)
# After condition fires, check takeover before processing queue
if self._dev_takeover:
    await self._handle_dev_takeover(input)
    return ...
```

### 6. `src/bug_bot/slack/handlers.py`

**Add takeover regex** (after `_CLOSE_RE`):
```python
_TAKEOVER_RE = re.compile(
    r'\b(take over|takeover|taking over|i will handle|i got this|i\'ll handle|'
    r'let me handle|handling this|i\'ll take it|i am taking|taking this|'
    r'i will look into|looking into this|i\'ll investigate|on it|i got it|'
    r'leave it to me|i\'ll pick this up|picking this up|assigning to myself)\b',
    re.IGNORECASE,
)
```

**Add `_is_takeover_intent` function** (mirroring `_is_close_intent`):
```python
async def _is_takeover_intent(text: str) -> bool:
    if not settings.anthropic_api_key:
        return True
    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            system=(
                "You determine whether a message is a developer explicitly claiming "
                "ownership of a bug investigation (taking it over from the bot). "
                "Reply with only 'yes' or 'no'."
            ),
            messages=[{"role": "user", "content": text}],
        )
        return response.content[0].text.strip().lower().startswith("yes")
    except Exception:
        logger.warning("Takeover-intent LLM check failed; falling back to regex result.")
        return True
```

**Modify `_handle_summary_thread_reply`:**

Step 1 — Before the existing close check, add a helper to test whether the workflow is active:
```python
async def _is_workflow_active(handle) -> bool:
    try:
        from temporalio.api.enums.v1 import WorkflowExecutionStatus
        desc = await handle.describe()
        return desc.status == WorkflowExecutionStatus.WORKFLOW_EXECUTION_STATUS_RUNNING
    except Exception:
        return False
```

Step 2 — Update close path to check workflow state first:
```python
if text and _CLOSE_RE.search(text) and await _is_close_intent(text):
    active = await _is_workflow_active(handle)
    if active:
        await handle.signal(BugInvestigationWorkflow.close_requested)
        # ... existing ack + reporter notification
    else:
        # Workflow already done — update DB directly
        async with async_session() as _s:
            repo = BugRepository(_s)
            await repo.update_status(bug.bug_id, "resolved")
            await repo.log_conversation(
                bug_id=bug.bug_id,
                message_type="resolved",
                sender_type="developer",
                sender_id=event.get("user"),
                channel=event["channel"],
                message_text=f"Closed by dev (workflow already ended): {text}",
            )
        await client.chat_postMessage(
            channel=event["channel"], thread_ts=thread_ts,
            text=f":white_check_mark: `{bug.bug_id}` marked as resolved."
        )
    return
```

Step 3 — Add takeover detection (before the normal dev message path):

Trigger condition: bot is @mentioned **and** takeover regex + LLM confirm intent.
```python
bot_mentioned = (
    settings.slack_bot_user_id and
    f"<@{settings.slack_bot_user_id}>" in (text or "")
)
if bot_mentioned and _TAKEOVER_RE.search(text) and await _is_takeover_intent(text):
    dev_user_id = event.get("user", "unknown")
    # Log conversation
    async with async_session() as session:
        await BugRepository(session).log_conversation(
            bug_id=bug.bug_id,
            message_type="dev_takeover",
            sender_type="developer",
            sender_id=dev_user_id,
            channel=event["channel"],
            message_text=text,
        )
    # Signal workflow (if active)
    active = await _is_workflow_active(handle)
    if active:
        await handle.signal(BugInvestigationWorkflow.dev_takeover, args=[dev_user_id])
    # Ack in #bug-summaries
    await client.chat_postMessage(
        channel=event["channel"], thread_ts=thread_ts,
        text=(
            f":handshake: Got it — <@{dev_user_id}> is taking over `{bug.bug_id}`. "
            "Bot will stand by and skip further automated investigations. "
            "Send a close message when done."
        ),
    )
    # Notify reporter in #bug-reports
    await client.chat_postMessage(
        channel=bug.slack_channel_id, thread_ts=bug.slack_thread_ts,
        text=f":wave: A developer has taken over `{bug.bug_id}` and is handling it directly.",
    )
    return

# Post-takeover dev messages (non-close): log only, no ack.
# The existing normal dev-message path at the bottom of this function handles this —
# it logs to DB and signals the workflow. The workflow's incoming_message signal
# handler still queues them, but _handle_dev_takeover's wait_condition ignores
# the queue (only wakes on close_requested), so they are effectively no-ops to
# the workflow loop. No separate handler needed.
```

---

## Files to Modify (summary)

| File | Change |
|------|--------|
| `models/models.py` | Add `assignee_user_id` column to `BugReport` |
| `db/repository.py` | Add `update_assignee()` method |
| `temporal/__init__.py` | Add `WorkflowState.DEV_TAKEOVER` |
| `temporal/activities/database_activity.py` | Add `update_bug_assignee` activity |
| `worker.py` | Register `update_bug_assignee` in activities list |
| `temporal/workflows/bug_investigation.py` | Add signal, flag, `_handle_dev_takeover`, loop checks |
| `slack/handlers.py` | Add `_TAKEOVER_RE`, `_is_takeover_intent`, `_is_workflow_active`, takeover detection, safe close path |

---

## DB Migration

A new nullable column `assignee_user_id VARCHAR(20)` needs to be added to `bug_reports`.
If Alembic is used, generate a migration. If raw DDL:
```sql
ALTER TABLE bug_reports ADD COLUMN assignee_user_id VARCHAR(20);
```

---

## Verification

1. Dev replies in #bug-summaries with `@BugBot let me take over this one` → bot acks in summary thread, reporter gets notified in #bug-reports, `bug_reports.assignee_user_id` is set, `bug_reports.status` is `dev_takeover`, no more Claude invocations.
2. Dev later sends another message in #bug-summaries (non-close) → logged as `dev_reply` conversation, no Claude, no ack change.
3. Dev sends close in #bug-summaries → existing close flow fires, workflow terminates cleanly.
4. Close message arrives after workflow already finished → DB updated and conversation logged without attempting to signal Temporal.
5. Takeover message without @BugBot mention → not treated as takeover, goes through normal dev message path.
