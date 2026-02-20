# Plan: @mention-gated Bug Reports Feature Flag

## Context

Currently, **every top-level message** posted to `#bug-reports` is treated as a bug report and kicks off a full investigation workflow. The team wants an optional mode where only messages that **explicitly @mention bugbot** trigger investigations. This makes it easier to have casual conversation in the channel without noisy false-positive investigations.

Both modes must coexist behind an env feature flag (`REQUIRE_BOT_MENTION`). Thread replies are completely unaffected in both modes.

---

## Files to Modify

| File | Change |
|------|--------|
| `src/bug_bot/config.py` | Add 2 new settings |
| `src/bug_bot/slack/handlers.py` | Add mention gate + mention stripping in top-level handler |

No other files need to change. The workflow, activities, prompts, tools, DB, and models are all unchanged.

---

## Implementation

### 1. `src/bug_bot/config.py`

Add two fields to the `Settings` class (after `bug_summaries_channel_id`):

```python
# Slack bot user ID (e.g. "U08XXXXX"). Required when require_bot_mention=True.
slack_bot_user_id: str = ""
# Feature flag: when True, only messages that @mention the bot in #bug-reports trigger an investigation.
# When False (default), all top-level messages in #bug-reports are treated as bug reports.
require_bot_mention: bool = False
```

Env vars: `SLACK_BOT_USER_ID` and `REQUIRE_BOT_MENTION=true/false`.

---

### 2. `src/bug_bot/slack/handlers.py`

**Location:** Inside `handle_message`, in the `# --- New top-level message in #bug-reports ---` branch (currently around line 120), **before** text extraction / triage.

**Logic to add:**

```python
# ── @mention gate (feature flag) ─────────────────────────────────────────
if settings.require_bot_mention and settings.slack_bot_user_id:
    mention_token = f"<@{settings.slack_bot_user_id}>"
    raw_text = event.get("text", "")
    # Also scan blocks in case the mention lives there
    block_text_for_scan = ""
    if event.get("blocks"):
        block_text_for_scan = _extract_text_from_blocks(event["blocks"])
    if mention_token not in raw_text and f"@{settings.slack_bot_user_id}" not in block_text_for_scan:
        return  # Not mentioned — ignore silently
```

**Mention stripping** — after the mention gate passes, strip the bot mention from `text` so it doesn't pollute the bug description sent to triage and the agent:

```python
# Strip bot mention from the text so it doesn't appear in the bug report
if settings.require_bot_mention and settings.slack_bot_user_id:
    import re
    text = re.sub(
        r'<@' + re.escape(settings.slack_bot_user_id) + r'(?:\|[^>]+)?>',
        '',
        text,
    ).strip()
```

This handles both `<@UBOTID>` and `<@UBOTID|bugbot>` variants that Slack may send.

**Where exactly:**
- The gate check goes **right after** the `if event.get("thread_ts"): return` guard (i.e., once we know this is a top-level message).
- The stripping goes **after** block text augmentation (after the `if event.get("blocks"): ...` block) and **before** `bug_id = f"BUG-..."`.

Everything after that point (attachment extraction, triage, DB save, Temporal workflow start) is **identical** for both modes — no duplication.

---

## Behaviour Matrix

| `require_bot_mention` | `slack_bot_user_id` | Message without mention | Message with @bugbot |
|-----------------------|---------------------|------------------------|----------------------|
| `False` (default) | any | ✅ triggers investigation | ✅ triggers investigation |
| `True` | set | ❌ silently ignored | ✅ triggers investigation (mention stripped from text) |
| `True` | empty | ✅ triggers investigation (gate disabled as guard) | ✅ triggers investigation |

Thread replies: **unchanged in all modes**.
File attachments in @mention messages: **fully supported** — extraction happens after the gate.

---

## Verification

1. **Default mode (`REQUIRE_BOT_MENTION=false`)** — post a plain message to `#bug-reports`. Confirm investigation starts as before.
2. **Mention mode enabled, message without @mention** — set `REQUIRE_BOT_MENTION=true`, `SLACK_BOT_USER_ID=<real ID>`. Post a plain message. Confirm no workflow starts, no ack posted.
3. **Mention mode enabled, message with @mention** — post `@bugbot payments are broken`. Confirm investigation starts and the stored `original_message` does NOT contain `<@UBOTID>`.
4. **File + mention** — share a file and type `@bugbot see attached logs`. Confirm attachment is extracted and investigation starts correctly.
5. **Thread replies unaffected** — in both modes, reply in an existing bug thread as the reporter. Confirm clarification signal still fires normally.
6. **`slack_bot_user_id` empty with flag on** — gate is skipped, all messages trigger (safe fallback).
