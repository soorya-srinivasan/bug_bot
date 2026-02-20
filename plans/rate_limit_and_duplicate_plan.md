# Plan: Reporter Rate Limiting + Duplicate Bug Auto-close

## Context

Two independent quality-of-life features to reduce token waste and reporter friction:

1. **Reporter rate limiting**: Reporters occasionally flood a bug thread with multiple messages before the agent finishes its continuation investigation. Each stored message becomes a Temporal signal that queues another LLM run — context rot and unnecessary token spend. Solution: track how many `reporter_reply` messages have been stored for the bug in the last N seconds; if above threshold, reject with a "please wait" reply without touching the DB.

2. **Duplicate detection**: Reporters sometimes file the same incident as multiple separate bugs. Each one starts a full investigation workflow. Solution: when a new bug is posted, check it semantically against recent open bugs using Claude (same pattern as `triage.py`). If a duplicate is found, skip DB insert + workflow start and tell the reporter to use the existing thread. Gated behind a feature flag.

No new DB tables or Alembic migrations are required for either feature.

---

## Files to Modify / Create

| File | Change |
|------|--------|
| `src/bug_bot/config.py` | Add 5 new settings (rate limit + duplicate detection) |
| `src/bug_bot/db/repository.py` | Add `count_recent_reporter_replies()` + `get_recent_open_bugs()` |
| `src/bug_bot/duplicate.py` | **New file** — Claude-based duplicate checker (follows `triage.py` pattern) |
| `src/bug_bot/slack/handlers.py` | Add rate-limit guard in `_handle_bug_thread_reply`; add duplicate-check in `handle_message` |

---

## 1. Config (`src/bug_bot/config.py`)

Add after the existing `summary_post_mode` field:

```python
# Reporter reply rate limiting
# Max replies a reporter may submit within reporter_reply_rate_window_secs before
# the message is silently dropped and a "please wait" reply is returned.
reporter_reply_rate_limit: int = 3
reporter_reply_rate_window_secs: int = 300  # 5 minutes

# Duplicate detection
# When True, new top-level bug reports are checked against recent open bugs.
enable_duplicate_detection: bool = False
# How far back to search for potential duplicates.
duplicate_check_window_hours: int = 2
# Minimum Claude-assessed similarity (0.0–1.0) to treat a report as duplicate.
duplicate_similarity_threshold: float = 0.8
```

---

## 2. Repository (`src/bug_bot/db/repository.py`)

### 2a. `count_recent_reporter_replies`

Append to `BugRepository`:

```python
async def count_recent_reporter_replies(self, bug_id: str, since: datetime) -> int:
    stmt = (
        select(func.count())
        .select_from(BugConversation)
        .where(
            BugConversation.bug_id == bug_id,
            BugConversation.sender_type == "reporter",
            BugConversation.message_type == "reporter_reply",
            BugConversation.created_at >= since,
        )
    )
    result = await self.session.execute(stmt)
    return int(result.scalar_one())
```

### 2b. `get_recent_open_bugs`

```python
async def get_recent_open_bugs(self, since: datetime) -> list[BugReport]:
    stmt = (
        select(BugReport)
        .where(
            BugReport.created_at >= since,
            BugReport.status.not_in(["resolved"]),
        )
        .order_by(BugReport.created_at.desc())
    )
    result = await self.session.execute(stmt)
    return list(result.scalars().all())
```

---

## 3. Duplicate Checker (`src/bug_bot/duplicate.py`) — New File

Follows the exact same structure as `triage.py`: one async function, Claude call, JSON response, safe defaults on error.

```python
"""Duplicate detection: compare a new bug report against recent open bugs."""
import json, logging
import anthropic
from bug_bot.config import settings

logger = logging.getLogger(__name__)

DUPLICATE_SYSTEM_PROMPT = """\
You are Bug Bot's duplicate detector for ShopTech.
Given a new bug report and a list of recent open bugs, determine whether the new
report describes the same root issue as any existing bug.

Respond with a JSON object (no markdown fences):
{
  "is_duplicate": true | false,
  "duplicate_of": "<bug_id>" | null,
  "confidence": 0.0-1.0
}

Rules:
- Only flag as duplicate if the core symptom and affected service(s) match.
- Ignore superficial wording differences; focus on what is broken and where.
- If no confident match, return is_duplicate: false.
"""


async def check_duplicate_bug(
    new_message: str,
    triage_summary: str,
    recent_bugs: list[dict],  # list of {"bug_id": str, "message": str}
) -> dict | None:
    """Return {"bug_id", "confidence"} if a duplicate is found, else None."""
    if not recent_bugs or not settings.anthropic_api_key:
        return None

    bug_list_text = "\n".join(
        f"- {b['bug_id']}: {b['message'][:300]}" for b in recent_bugs
    )
    prompt = (
        f"New report (triage summary: {triage_summary}):\n{new_message}\n\n"
        f"Recent open bugs:\n{bug_list_text}"
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",  # cheapest model; this is a quick check
            max_tokens=150,
            system=DUPLICATE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0].strip()
        result = json.loads(text)
        if result.get("is_duplicate") and result.get("duplicate_of"):
            return {"bug_id": result["duplicate_of"], "confidence": result.get("confidence", 0.0)}
    except Exception:
        logger.exception("Duplicate check failed; treating as non-duplicate.")
    return None
```

---

## 4. Slack Handlers (`src/bug_bot/slack/handlers.py`)

### 4a. Rate limiting in `_handle_bug_thread_reply`

Add the check **after** the existing reporter-filter and active-status checks (line ~287), and **before** the `log_conversation()` call (currently around line 322):

```python
# ── Rate limiting ─────────────────────────────────────────────────────────
from datetime import timedelta
rate_window_start = datetime.utcnow() - timedelta(seconds=settings.reporter_reply_rate_window_secs)
async with async_session() as _s:
    recent_count = await BugRepository(_s).count_recent_reporter_replies(
        bug.bug_id, since=rate_window_start
    )
if recent_count >= settings.reporter_reply_rate_limit:
    await client.chat_postMessage(
        channel=channel_id,
        thread_ts=thread_ts,
        text=(
            ":hourglass: We've received your previous messages and the investigation "
            "is catching up. Please wait a moment before sending more updates."
        ),
    )
    return
```

`datetime` is already used in `repository.py` but needs to be imported in `handlers.py` — add `from datetime import datetime, timedelta` at the top.

### 4b. Duplicate detection in `handle_message`

Add import at top of file:
```python
from bug_bot.duplicate import check_duplicate_bug
```

Insert after the `triage = await triage_bug_report(...)` call (currently line 164) and before the `client.chat_postMessage` acknowledgement (line 169):

```python
# ── Duplicate detection (feature flag) ────────────────────────────────────
if settings.enable_duplicate_detection:
    from datetime import timedelta
    dup_since = datetime.utcnow() - timedelta(hours=settings.duplicate_check_window_hours)
    async with async_session() as _s:
        recent_bugs = await BugRepository(_s).get_recent_open_bugs(since=dup_since)
    candidates = [
        {"bug_id": b.bug_id, "message": b.original_message}
        for b in recent_bugs
    ]
    dup = await check_duplicate_bug(text, triage.get("summary", ""), candidates)
    if dup and dup["confidence"] >= settings.duplicate_similarity_threshold:
        dup_bug_id = dup["bug_id"]
        # Look up original thread for a deep link
        async with async_session() as _s:
            orig = await BugRepository(_s).get_bug_by_id(dup_bug_id)
        thread_link = (
            f"slack://channel?team=&id={orig.slack_channel_id}&thread_ts={orig.slack_thread_ts}"
            if orig else ""
        )
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=(
                f":warning: This report looks similar to an existing open bug: *{dup_bug_id}*.\n"
                f"Please add your information to that thread"
                + (f": <{thread_link}|view thread>" if thread_link else ".")
                + "\nIf your issue is genuinely different, re-post with more specific details."
            ),
        )
        return  # Do NOT insert to DB or start workflow
```

`datetime` import: already needed for rate limiting; add once at top of handlers.py.

---

## Behaviour After Implementation

**Rate limiting:**
- Reporter can send up to `REPORTER_REPLY_RATE_LIMIT` (default 3) messages within `REPORTER_REPLY_RATE_WINDOW_SECS` (default 300 s) before further messages are silently dropped with a "please wait" reply.
- Messages that pass the check are stored and forwarded exactly as today.
- Both thresholds are configurable via `.env`.

**Duplicate detection:**
- Off by default (`ENABLE_DUPLICATE_DETECTION=false`).
- When enabled, new bug reports are checked against bugs created in the last `DUPLICATE_CHECK_WINDOW_HOURS` (default 2) that are not yet resolved.
- Uses `claude-haiku-4-5-20251001` (cheap + fast) for the similarity call.
- If Claude returns `confidence >= DUPLICATE_SIMILARITY_THRESHOLD` (default 0.8), the message is acknowledged with a pointer to the original bug — no DB row created, no workflow started.
- Check failure (API error, JSON parse error) falls back to treating the report as non-duplicate, so the system degrades gracefully.

---

## Verification

### Rate limiting
1. Post a bug report in #bug-reports.
2. Reply to the thread 4+ times quickly as the original reporter.
3. Confirm the 4th+ replies receive the "please wait" message in Slack and do NOT appear as rows in `bug_conversations`.
   ```sql
   SELECT * FROM bug_conversations WHERE bug_id = 'BUG-xxx' ORDER BY created_at;
   ```
4. Change `REPORTER_REPLY_RATE_LIMIT=1` in `.env`, restart, verify threshold fires after 1 reply.

### Duplicate detection
1. Set `ENABLE_DUPLICATE_DETECTION=true` in `.env`, restart.
2. Post a bug: "Payment API returning 500 for all checkout requests."
3. Post a near-identical bug within 2 hours: "Checkout is broken, getting 500 from payment service."
4. Confirm the second post gets a duplicate warning in Slack and no row is created in `bug_reports`.
   ```sql
   SELECT bug_id, original_message, created_at FROM bug_reports ORDER BY created_at DESC LIMIT 5;
   ```
5. Set `ENABLE_DUPLICATE_DETECTION=false`, confirm it is bypassed entirely.
