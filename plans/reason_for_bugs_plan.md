# Resolution Tracking for Bug Closure

## Context

Currently, when a dev closes a bug via Slack, the bot immediately closes it with no structured data about how/why it was resolved. This makes it hard to track resolution patterns (code fix vs data fix vs invalid bug) and understand what fixes were applied. This change requires devs to provide resolution details before a bug can be closed from `#bug-summaries`.

## Scope

- **Dev-initiated closure** (from `#bug-summaries`): **must** provide `resolution_type` + `closure_reason` before closure proceeds
- **Reporter-initiated closure** (from `#bug-reports`): unchanged, no details required
- **Admin panel / API**: accepts fields optionally
- **Auto-close (timeouts)**: unchanged, no details required
- **`!resolve` / `!close` / `!fixed` shortcut** (handlers.py:335): also requires resolution details

---

## 1. Database: Add 3 columns to `bug_reports`

**File: `src/bug_bot/models/models.py`** (after `resolved_at`, line 33)

Add three nullable columns:
```python
resolution_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
# values: "code_fix" | "data_fix" | "sre_fix" | "not_a_valid_bug"
closure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
fix_provided: Mapped[str | None] = mapped_column(Text, nullable=True)
```

**New alembic migration** (`alembic/versions/`):
- `down_revision = "e9f1a2b3c4d7"`
- Add 3 columns + index on `resolution_type`

---

## 2. Repository Layer

**File: `src/bug_bot/db/repository.py`**

**New method** `update_resolution_details(bug_id, *, resolution_type, closure_reason, fix_provided)`:
- Sets the three resolution fields on a bug_reports row
- Called by the Slack handler before signaling close

**New method** `has_pending_closure_request(bug_id) -> bool`:
- Checks `BugConversation` for a `message_type="closure_details_requested"` entry for this bug
- Used to detect when a dev replies with details after the bot asked for them

**Extend** `update_bug_admin()` (line 130):
- Accept optional `resolution_type`, `closure_reason`, `fix_provided` params
- Include in update values when provided

---

## 3. Slack Handlers (core logic)

**File: `src/bug_bot/slack/handlers.py`**

### 3a. New LLM function: `_extract_resolution_details(text) -> dict | None`

Uses Claude Haiku to extract structured data from a dev's natural language close message:
```json
{
  "resolution_type": "code_fix" | "data_fix" | "sre_fix" | "not_a_valid_bug" | null,
  "closure_reason": "string or null",
  "fix_provided": "string or null"
}
```
Follows the same pattern as existing `_is_close_intent()` (Haiku, fail-safe).

### 3b. Modify dev-closure block in `_handle_summary_thread_reply()` (lines 575-622)

Current flow: detect close intent -> immediately close

New flow:
1. Detect close intent (unchanged three-layer check)
2. Call `_extract_resolution_details(text)`
3. **If `resolution_type` AND `closure_reason` present**:
   - Save resolution details to `bug_reports` via `repo.update_resolution_details()`
   - Proceed with closure (audit log, signal/DB update, ack with resolution summary)
4. **If either missing**:
   - Post message listing what's missing (resolution type, closure reason)
   - Log `message_type="closure_details_requested"` in `BugConversation`
   - **Return without closing**

### 3c. New block: handle follow-up after bot asked for details

Insert after the close-intent block, before the dev-takeover block (~line 624):

1. Check `has_pending_closure_request(bug_id)` for messages that aren't detected as close intent
2. If pending and bug not resolved: extract resolution details from the reply
3. If details now complete: save and close (same path as 3b success)
4. If still incomplete: re-prompt with what's still missing

### 3d. Modify `!resolve` / `!close` / `!fixed` handler (lines 335-359)

Current: immediately marks bug as resolved with no details.

New flow (same pattern as 3b):
1. Call `_extract_resolution_details(text)` on the message text after the command
2. If the command has no extra text or details are missing: post message asking for resolution_type, closure_reason, and fix_provided. Log `closure_details_requested`. Return without closing.
3. If details present: save resolution details, then proceed with closure.
4. Follow-up replies handled by the same pending-closure check (3c) if the command was used in a summary thread.

Note: Since `!resolve` can be used in `#bug-reports` threads too (it matches on `thread_ts`), we'll apply the same requirement regardless of channel for this command.

### 3e. Reporter closure (`_handle_bug_thread_reply`, lines 411-442): NO CHANGES

---

## 4. Temporal Activity + Worker

**File: `src/bug_bot/temporal/activities/database_activity.py`**
- New activity `update_resolution_details(bug_id, resolution_type, closure_reason, fix_provided)` (for future workflow use)

**File: `src/bug_bot/worker.py`**
- Register the new activity

**No changes to** `BugInvestigationWorkflow` or the `close_requested` signal. Resolution details are saved to DB by the handler before the signal is sent.

---

## 5. Schemas + API

**File: `src/bug_bot/schemas/admin.py`**
- Add `ResolutionType = Literal["code_fix", "data_fix", "sre_fix", "not_a_valid_bug"]`
- Extend `BugUpdate` with optional `resolution_type`, `closure_reason`, `fix_provided`
- Extend `BugListItem` with these three fields

**File: `src/bug_bot/api/admin.py`**
- Pass resolution fields through in `update_bug()` PATCH endpoint
- Include in audit log payload when closing via admin
- Add fields to all `BugListItem` constructions (list, detail, update responses)

**File: `src/bug_bot/api/routes.py`**
- Add optional `ResolveBugRequest` body to `POST /resolve-bug/{bug_id}`

---

## Files to Modify

| File | Change |
|------|--------|
| `src/bug_bot/models/models.py` | Add 3 columns to `BugReport` |
| `alembic/versions/<new>.py` | New migration |
| `src/bug_bot/db/repository.py` | `update_resolution_details()`, `has_pending_closure_request()`, extend `update_bug_admin()` |
| `src/bug_bot/slack/handlers.py` | `_extract_resolution_details()`, modify dev-closure block, add pending-closure follow-up |
| `src/bug_bot/temporal/activities/database_activity.py` | New activity |
| `src/bug_bot/worker.py` | Register activity |
| `src/bug_bot/schemas/admin.py` | Extend schemas |
| `src/bug_bot/api/admin.py` | Wire resolution fields |
| `src/bug_bot/api/routes.py` | Optional request body |

## Implementation Order

1. Model + migration
2. Repository methods
3. Schemas
4. Slack handlers (core logic)
5. Temporal activity + worker
6. API/admin endpoints

## Verification

1. **Slack test (missing details)**: Send `@bugbot close this` in a bug summary thread -> bot should ask for resolution_type and closure_reason, bug should NOT close
2. **Slack test (follow-up)**: Reply with `code_fix, fixed the null pointer in PaymentService` -> bot should extract details and close the bug
3. **Slack test (full details in one message)**: Send `@bugbot close this, code_fix, fixed config issue for payment retry. RCA: https://...` -> bug should close immediately with details stored
4. **Reporter test**: Reporter sends `@bugbot close this` in bug-reports thread -> should close immediately with no details required (existing behavior)
5. **Admin panel**: PATCH `/bugs/{bug_id}` with `status: "resolved", resolution_type: "data_fix"` -> should update all fields
6. **DB check**: Verify `resolution_type`, `closure_reason`, `fix_provided` columns are populated on resolved bugs
