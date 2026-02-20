# Bug Bot — Conversation-Driven Workflow Redesign

## Context

The current `BugInvestigationWorkflow` uses ad-hoc state: boolean flags (`_awaiting_clarification`, `_reporter_close`), a sentinel string (`[CLOSE_REQUESTED]`), and two separate message buckets (`_reporter_info` for clarification answers, `_reporter_queue` for "context" messages). This causes two real bugs:

1. **Dev messages during follow-up are silently dropped** — `_dev_reply` is a single dict overwritten on every signal; only one message survives per wait iteration.
2. **Intent guessing in the handler** — `_detect_intent()` regex pre-classifies dev messages as "approve/context" before the agent ever sees them; wrong classifications cause wrong prompts.

The goal is a **unified conversation queue + explicit state machine** where all parties can message at any time, no messages are ever dropped, the agent always has full context via session resumption, and the workflow routing is driven by an explicit `action` field in the agent's output.

---

## Approach

Replace 5 state vars and 3 signals with 3 state vars and 2 signals. Add an explicit `action` field to the agent output schema. Replace `run_followup` with `run_continuation` that accepts all queued messages and builds a rich prompt. The `claude_session_id` thread through every continuation turn ensures Claude has full investigation history.

**Conversation persistence + DB-backed tool (new):** Every incoming message is flushed to `bug_conversations` before the agent runs — not in the signal handler (Temporal signals cannot call activities), but immediately before each agent activity. The agent is given a `get_bug_conversations(bug_id)` MCP tool so it can proactively read the full conversation history at the start of each turn. This means:
- A reporter's second message (sent while the agent is still running) is captured in DB and visible to the agent in the next turn via the tool, even if it wasn't in the drain list.
- The agent can see all prior messages (reporter, developer, bot) and decide whether it still needs to ask for clarification or if the answer is already in the thread.
- `sender_type` is already a column in `bug_conversations` — no schema migration required.

---

## File-by-File Plan

### 1. `src/bug_bot/temporal/__init__.py`
Add two new types (existing dataclasses unchanged):

```python
from enum import Enum

class WorkflowState(str, Enum):
    INVESTIGATING     = "investigating"
    AWAITING_REPORTER = "awaiting_reporter"
    AWAITING_DEV      = "awaiting_dev"

@dataclass
class IncomingMessage:
    sender_type: str   # "reporter" | "developer"
    sender_id: str     # Slack user ID
    text: str
```

`WorkflowState` is a `str` enum so Temporal serialises it cleanly. `IncomingMessage` has no timestamp — Temporal preserves signal order.

---

### 2. `src/bug_bot/temporal/workflows/bug_investigation.py`
**Full rewrite.** Key changes:

**Signals (replaces `dev_reply`, `reporter_info`, `reporter_close`):**
```python
@workflow.signal
async def incoming_message(self, sender_type: str, sender_id: str, text: str) -> None:
    self._message_queue.append(IncomingMessage(sender_type, sender_id, text))

@workflow.signal
async def close_requested(self) -> None:
    self._close_requested = True
```

**State (replaces 5 old vars):**
```python
_state: WorkflowState = WorkflowState.INVESTIGATING
_message_queue: list[IncomingMessage] = []
_close_requested: bool = False
```

**State-aware wait conditions:**
- `AWAITING_REPORTER` (2 h): unblocks only when `sender_type == "reporter"` arrives **or** `_close_requested`
- `AWAITING_DEV` (48 h): unblocks on any message **or** `_close_requested`

Dev messages that arrive during `AWAITING_REPORTER` queue up and are included in the next agent turn automatically. Nothing is dropped.

**Workflow loop (pseudo-code):**
```
parse_bug_report → run_agent_investigation → get action/session_id

loop:
  if _close_requested → handle_close, return

  if action == "ask_reporter":
    post clarification question to #bug-reports
    _state = AWAITING_REPORTER
    wait_condition(reporter message OR close, timeout=2h)
    _state = INVESTIGATING
    if close or timeout → handle_close/proceed
    messages = drain_queue()
    log all messages
    result = run_continuation_investigation(messages, state="awaiting_reporter", session_id)
    session_id = result.claude_session_id
    continue

  save_investigation_result
  post_investigation_results
  create_summary_thread

  if action == "resolved":
    update_status "resolved" → cleanup_workspace → return

  if action == "escalate":
    escalate_to_humans → start SLATrackingWorkflow child

  update_status "pending_verification"
  _state = AWAITING_DEV
  wait_condition(any message OR close, timeout=48h)
  _state = INVESTIGATING

  if close → handle_close, return
  if timeout → update_status "resolved" → cleanup, return

  messages = drain_queue()
  log all messages
  post ":mag: Follow-up investigation started..."
  result = run_continuation_investigation(messages, state="awaiting_dev", session_id)
  session_id = result.claude_session_id
  # loop back → re-evaluate action from new result
```

Remove all `print()` debug statements. Remove `_handle_close` helper complexity — inline the 3-line close sequence.

---

### 3. `src/bug_bot/agent/runner.py`

**Add `action` to `_OUTPUT_SCHEMA`:**
```json
"action": {
  "type": "string",
  "enum": ["ask_reporter", "post_findings", "escalate", "resolved"]
}
```

**Add to `_SYSTEM_PROMPT`** (one short paragraph):
```
At the end of each turn, set the "action" field:
- "ask_reporter": need more info from reporter (also set clarification_question)
- "post_findings": have findings, want developer review before creating a fix
- "resolved": bug is fully resolved or confirmed non-issue
- "escalate": requires human engineers (complex, security, or infra-level issue)
```

**Replace `run_followup` with `run_continuation`:**
```python
async def run_continuation(
    bug_id: str,
    messages: list[dict],   # [{"sender_type", "sender_id", "text"}, ...]
    state: str,             # WorkflowState string value
    claude_session_id: str | None = None,
) -> dict:
```
- Prompt built via `build_continuation_prompt(bug_id, messages, state)` (new function in prompts.py)
- Session resumed via `_build_options(resume=claude_session_id, cwd=workspace)` — unchanged
- Rest of the function (thread pool, error fallback, cost/duration enrichment) identical to current `run_followup`

Delete `run_followup` entirely.

---

### 4. `src/bug_bot/agent/prompts.py`

Add alongside existing `build_investigation_prompt`:

```python
_SENDER_LABEL = {"reporter": "Reporter", "developer": "Developer"}

def build_continuation_prompt(bug_id: str, messages: list[dict], state: str) -> str:
    lines = [
        f"- [{_SENDER_LABEL.get(m['sender_type'], m['sender_type'].title())}]: {m['text']}"
        for m in messages
    ]
    body = "New messages since your last investigation turn:\n\n" + "\n".join(lines)

    if state == WorkflowState.AWAITING_REPORTER:
        instruction = (
            "The first message above is the reporter's answer to your clarification question. "
            "Incorporate it and continue the investigation."
        )
    elif state == WorkflowState.AWAITING_DEV:
        instruction = (
            "If a developer message asks you to create a fix or open a PR, do so now. "
            "Otherwise, use the additional context to refine your analysis."
        )
    else:
        instruction = "Continue your investigation with this additional context."

    return f"{body}\n\n{instruction}"
```

`WorkflowState` import: use string comparison (e.g. `state == "awaiting_reporter"`) to avoid circular import, or import from `bug_bot.temporal`.

---

### 5. `src/bug_bot/temporal/activities/agent_activity.py`

**Rename and update signature:**
```python
# DELETE: run_followup_investigation
# ADD:
@activity.defn
async def run_continuation_investigation(
    bug_id: str,
    messages: list[dict],
    state: str,
    claude_session_id: str | None = None,
) -> dict:
```

Error fallback dict must include `"action": "escalate"` so the workflow's action dispatch never KeyErrors.

`_run_with_heartbeat` reused unchanged.

---

### 6. `src/bug_bot/worker.py`

Swap one import and one entry in the activities list:
```python
# Remove: run_followup_investigation
# Add: run_continuation_investigation
```

---

### 7. `src/bug_bot/slack/handlers.py`

**`_handle_bug_thread_reply` (reporter channel):**
```python
# close intent (unchanged detection logic):
await handle.signal(BugInvestigationWorkflow.close_requested)

# normal reply:
await handle.signal(
    BugInvestigationWorkflow.incoming_message,
    args=["reporter", event.get("user", "unknown"), text],
)
```

**`_handle_summary_thread_reply` (dev channel):**
```python
# All dev messages — no intent pre-classification:
await handle.signal(
    BugInvestigationWorkflow.incoming_message,
    args=["developer", event.get("user", "unknown"), text],
)
# Generic ack: ":mag: Your message has been forwarded to the investigation."
```

**Remove:** `_detect_intent()`, `_APPROVE_RE` regex, intent-specific ack strings.
**Keep:** `_CLOSE_RE` regex and close detection (reporter close bypasses the queue and triggers clean shutdown).

---

### 8. `src/bug_bot/db/repository.py`

Add a read method (no schema change needed — `bug_conversations` and `sender_type` already exist):

```python
async def get_conversations(self, bug_id: str) -> list[BugConversation]:
    result = await self.session.execute(
        select(BugConversation)
        .where(BugConversation.bug_id == bug_id)
        .order_by(BugConversation.created_at)
    )
    return list(result.scalars().all())
```

---

### 9. `src/bug_bot/agent/tools.py`

Add `get_bug_conversations` tool using sync psycopg (same pattern as existing tools):

```python
def _get_bug_conversations_sync(bug_id: str) -> str:
    with psycopg.connect(_bugbot_conninfo(), row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sender_type, sender_id, message_type, message_text, created_at
                FROM bug_conversations
                WHERE bug_id = %s
                ORDER BY created_at;
                """,
                (bug_id,),
            )
            rows = cur.fetchall()
    if not rows:
        return f"No conversation history found for {bug_id}."
    lines = []
    for r in rows:
        ts = r["created_at"].strftime("%H:%M:%S") if r["created_at"] else "?"
        lines.append(
            f"[{ts}] {r['sender_type'].upper()} ({r['message_type']}): {r['message_text'] or ''}"
        )
    return "\n".join(lines)

@tool(
    "get_bug_conversations",
    "Retrieve the full conversation history for a bug (reporter messages, developer replies, bot findings). Call this at the start of each continuation turn to get full context before deciding on next steps.",
    {"bug_id": str},
)
async def get_bug_conversations(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(_get_bug_conversations_sync, args["bug_id"])
    return _text_result(text)
```

Register in `build_custom_tools_server()` tools list.

---

### 10. Workflow: flush queue to DB before each agent turn

In `bug_investigation.py`, before every call to `run_continuation_investigation`, flush `_message_queue` to DB:

```python
# Flush queued messages to DB before running the agent
messages_to_flush = list(self._message_queue)
self._message_queue.clear()
for msg in messages_to_flush:
    await workflow.execute_activity(
        log_conversation_event,
        args=[input.bug_id, "incoming_message", msg.sender_type,
              msg.sender_id, None, msg.text, None],
        start_to_close_timeout=timedelta(seconds=10),
    )
# Pass the flushed messages as the prompt context
result = await workflow.execute_activity(
    run_continuation_investigation,
    args=[input.bug_id, [m.__dict__ for m in messages_to_flush], state, claude_session_id],
    ...
)
```

Because signals cannot call activities (Temporal constraint), the queue is the in-memory buffer. Flushing before every agent activity achieves the same effect as "storing in DB during the signal" — the DB is always up to date before the agent ever makes a tool call.

---

### 11. `src/bug_bot/agent/prompts.py` — updated continuation instruction

```python
def build_continuation_prompt(bug_id: str, messages: list[dict], state: str) -> str:
    lines = [
        f"- [{_SENDER_LABEL.get(m['sender_type'], m['sender_type'].title())}]: {m['text']}"
        for m in messages
    ]
    recent = "Recent messages since your last turn:\n\n" + "\n".join(lines) if lines else ""
    instruction = (
        f"Start by calling get_bug_conversations(\"{bug_id}\") to review the full "
        "conversation history for this bug. Then incorporate the context and continue "
        "the investigation. If the reporter already answered a pending clarification "
        "question, do not ask again — use the recorded answer."
    )
    if state == "awaiting_reporter":
        instruction += (
            " The reporter's response should be the most recent reporter message in the history."
        )
    elif state == "awaiting_dev":
        instruction += (
            " If a developer message requests a fix or PR, proceed with creating it."
        )
    return f"{recent}\n\n{instruction}".strip()
```

---

## What Does NOT Change

- `_run_with_heartbeat` — reused as-is
- `_build_options`, `_run_sdk_sync`, `_collect_response` in runner.py — unchanged
- `run_investigation` (initial investigation) — unchanged
- `run_agent_investigation` activity — unchanged
- `cleanup_workspace` activity — unchanged
- All database activities, Slack activities — unchanged
- `SLATrackingWorkflow` — unchanged
- All other psycopg tools in `tools.py` — unchanged
- `BugRepository` core methods, all models — unchanged
- `build_investigation_prompt` — unchanged
- `bug_conversations` schema — no migration needed (`sender_type` column already exists)

---

## Backward Compatibility

Dev environment only. Any workflow running against the old signal names (`dev_reply`, `reporter_info`, `reporter_close`) will simply receive no more signals and auto-resolve at the 48-hour timeout. Terminate running workflows via Temporal UI before deploying if a clean cut-over is needed.

---

## Verification

1. **Start the worker** — `python -m bug_bot.worker`
2. **Send a vague bug** — e.g. "something is broken". Confirm agent returns `action: "ask_reporter"` and posts a question in #bug-reports.
3. **Reply as reporter** — check workflow unblocks, continuation runs, and updated findings post to #bug-summaries.
4. **Reporter sends a second message mid-investigation** — while the agent is running its continuation, reporter sends another message. Confirm it flushes to DB before the next agent turn and the agent sees it via `get_bug_conversations`.
5. **Reply as a third party** (non-reporter) in the #bug-reports thread — confirm nothing happens (silent filter still applies).
6. **Reply from dev in #bug-summaries** — send two dev messages quickly. Confirm both appear in the conversation history the agent reads.
7. **Agent skips clarification** — if the reporter's second message already answers a potential follow-up question, confirm agent does not ask again (reads history first).
8. **Close intent** — reporter replies "never mind". Confirm workflow closes, workspace is cleaned up, status is "resolved".
9. **Temporal UI** — confirm workflow history shows clean state transitions: `investigating → awaiting_reporter → investigating → awaiting_dev → resolved`.
