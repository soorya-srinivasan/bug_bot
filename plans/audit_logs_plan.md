# Plan: Bug Audit Logs

## Context

Important actions like priority updates, dev takeovers, and bug closures currently lack a dedicated audit trail. The existing `bug_conversations` table logs Slack messages and some events, but it's designed for conversation threading — not structured audit records with action types, payloads, and source tracking. We need a purpose-built `bug_audit_logs` table that captures **who** did **what** from **where**, with structured payloads.

**Scope:** Track 3 action types across all sources (admin panel, Slack, API, system):
1. `priority_updated` — severity changes (P1–P4)
2. `dev_takeover` — developer claims ownership
3. `bug_closed` — bug resolved/closed

**`performed_by` is nullable** — no auth yet, will wire up later.

---

## Files to Change

| File | Change |
|------|--------|
| `src/bug_bot/models/models.py` | Add `BugAuditLog` model (after `BugConversation`, line ~159) |
| `alembic/versions/<id>_add_bug_audit_logs_table.py` | **New** — migration for table + indexes |
| `src/bug_bot/db/repository.py` | Add `create_audit_log()` + `get_audit_logs()` to `BugRepository` |
| `src/bug_bot/schemas/admin.py` | Add `AuditLogResponse` + `AuditLogListResponse` |
| `src/bug_bot/api/admin.py` | Add `GET /bugs/{bug_id}/audit-logs`; instrument `PATCH /bugs/{bug_id}` |
| `src/bug_bot/api/routes.py` | Instrument `POST /resolve-bug/{bug_id}` |
| `src/bug_bot/slack/handlers.py` | Instrument dev_takeover + close handlers (4 sites) |
| `src/bug_bot/temporal/activities/database_activity.py` | Add `log_audit_event` activity; instrument `mark_bug_auto_closed` |
| `src/bug_bot/temporal/workflows/bug_investigation.py` | Instrument 48h auto-resolve + 7-day safety-net timeout |
| `src/bug_bot/worker.py` | Register `log_audit_event` activity |

---

## Implementation

### 1. Model — `BugAuditLog`

Add after `BugConversation` (line 159 in `models.py`):

```python
class BugAuditLog(Base):
    __tablename__ = "bug_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)        # priority_updated | dev_takeover | bug_closed
    source: Mapped[str] = mapped_column(String(20), nullable=False)        # admin_panel | slack | api | system
    performed_by: Mapped[str | None] = mapped_column(String(50), nullable=True)  # Slack user ID or None
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_bug_audit_logs_bug_id", "bug_id"),
        Index("idx_bug_audit_logs_action", "action"),
    )
```

### 2. Alembic Migration

New file. `down_revision = '7d4399b0b6d6'`. Creates `bug_audit_logs` table with columns + 2 indexes.

### 3. Repository Methods

Add `BugAuditLog` to the import in `repository.py` (line 7–11). Add two methods after `get_conversations` (~line 510):

```python
async def create_audit_log(self, bug_id, action, source, *, performed_by=None, payload=None, metadata=None) -> BugAuditLog:
    entry = BugAuditLog(bug_id=bug_id, action=action, source=source,
                        performed_by=performed_by, payload=payload, metadata_=metadata)
    self.session.add(entry)
    await self.session.commit()
    return entry

async def get_audit_logs(self, bug_id: str) -> list[BugAuditLog]:
    stmt = select(BugAuditLog).where(BugAuditLog.bug_id == bug_id).order_by(BugAuditLog.created_at)
    result = await self.session.execute(stmt)
    return list(result.scalars().all())
```

### 4. Pydantic Schemas

Add after `BugConversationListResponse` (line ~389 in `schemas/admin.py`):

```python
class AuditLogResponse(BaseModel):
    id: str
    bug_id: str
    action: str
    source: str
    performed_by: str | None = None
    payload: dict | None = None
    metadata: dict | None = None
    created_at: datetime

class AuditLogListResponse(BaseModel):
    items: list[AuditLogResponse]
    total: int
```

### 5. API — GET Endpoint + PATCH Instrumentation

**5a. GET `/bugs/{bug_id}/audit-logs`** (in `admin.py`, after `get_bug_conversations` at line 471):

Follows the exact `get_bug_conversations` pattern — fetch bug, 404 if missing, query `get_audit_logs`, map to response.

**5b. Instrument `PATCH /bugs/{bug_id}`** (lines 238–290):

Insert between lines 254–256, **before** the `update_bug_admin` call, capture old values. Then after the update:

- If `payload.severity` differs from `bug.severity` → log `priority_updated` with `{"previous_severity": ..., "new_severity": ...}`
- If `payload.status == "resolved"` and `bug.status != "resolved"` → log `bug_closed` with `{"previous_status": ..., "reason": "Resolved via admin panel"}`

Source: `"admin_panel"`, `performed_by`: `None` (no auth yet).

### 6. API Routes — Instrument `resolve_bug`

In `routes.py`, extend the second `async with` block (line 46–55). After the existing `log_conversation` call, add:

```python
await repo.create_audit_log(
    bug_id=bug_id, action="bug_closed", source="api",
    payload={"previous_status": bug.status, "reason": "Resolved via API call"},
)
```

`bug.status` is captured from the first session block (line 20) before the update.

### 7. Slack Handlers — 4 Instrumentation Sites

All in `handlers.py`. Each adds a `repo.create_audit_log()` call alongside the existing `repo.log_conversation()` in the same session:

| Site | Lines | Action | Source |
|------|-------|--------|--------|
| Reporter close | 417–426 | `bug_closed` | `slack` |
| Dev close (workflow active) | 570–573 | `bug_closed` | `slack` |
| Dev close (workflow ended) | 574–586 | `bug_closed` | `slack` |
| Dev takeover | 607–618 | `dev_takeover` | `slack` |

For each, extend the existing `async with async_session()` block to create a `BugRepository` variable (instead of inline) so we can call both `log_conversation` and `create_audit_log` in the same transaction.

### 8. Temporal Activity — `log_audit_event`

Add to `database_activity.py` (after `log_conversation_event`):

```python
@activity.defn
async def log_audit_event(bug_id: str, action: str, source: str,
                          performed_by: str | None = None, payload: dict | None = None,
                          metadata: dict | None = None) -> None:
    async with async_session() as session:
        await BugRepository(session).create_audit_log(
            bug_id=bug_id, action=action, source=source,
            performed_by=performed_by, payload=payload, metadata=metadata,
        )
    activity.logger.info(f"Audit log created for {bug_id}: action={action}")
```

Also instrument `mark_bug_auto_closed` — fetch the bug first to capture `previous_status`, then add `create_audit_log` call with `action="bug_closed"`, `source="system"`.

### 9. Worker Registration

In `worker.py`: import `log_audit_event`, add to `activities=[...]` list.

### 10. Workflow — System Auto-Resolve Paths

In `bug_investigation.py`, add `log_audit_event` to the imports in `workflow.unsafe.imports_passed_through()`. Instrument two auto-resolve paths:

- **48h timeout** (line 361–372): Add `log_audit_event` activity call after `update_bug_status`, `source="system"`, payload includes `"reason": "Auto-resolved after 48-hour timeout"`
- **7-day safety net** (line 448–458): Same pattern, reason = `"Auto-resolved after 7-day dev takeover timeout"`

**Skip** `_handle_close` and `_handle_dev_takeover` — those are triggered by Slack signals which already have audit logs at the handler level.

---

## What's NOT Changed

- `bug_conversations` — continues to work as-is for conversation tracking
- Existing Slack message handling logic — only extended with audit calls
- No auth changes — `performed_by` stays nullable

## Verification

1. Run `alembic upgrade head` → table `bug_audit_logs` created with indexes
2. Via admin panel: `PATCH /api/admin/bugs/{bug_id}` with `{"severity": "P1"}` → query `GET /api/admin/bugs/{bug_id}/audit-logs` → see `priority_updated` entry
3. Via admin panel: `PATCH /api/admin/bugs/{bug_id}` with `{"status": "resolved"}` → see `bug_closed` entry
4. Via API: `POST /api/resolve-bug/{bug_id}` → see `bug_closed` entry with `source: "api"`
5. Via Slack: dev mentions bot with "I'll take over" → see `dev_takeover` entry with `source: "slack"`
6. Via Slack: reporter mentions bot with "close this" → see `bug_closed` with `source: "slack"`
7. Wait for auto-close workflow to fire → see `bug_closed` with `source: "system"`
