# Plan: Implement `report_finding` Tool with Persistent Storage

## Context

`report_finding` is called by the Claude agent during investigations to log key observations (errors found, metric anomalies, service health issues, etc.). Currently it is a **no-op placeholder** — it returns a fake confirmation string and discards the data. The data needs to be persisted so it can be used to evaluate agent quality and eventually surfaced in the admin bug-case view.

---

## Files to Modify / Create

| File | Change |
|------|--------|
| `src/bug_bot/models/models.py` | Add `InvestigationFinding` ORM model |
| `src/bug_bot/db/repository.py` | Add `save_finding()` + `get_findings_for_bug()` methods |
| `src/bug_bot/agent/tools.py` | Add `_report_finding_sync()`, implement `report_finding` tool, extend schema with `bug_id` |
| `alembic/versions/<new_hash>_add_investigation_findings_table.py` | New migration |

---

## 1. New DB Table — `investigation_findings`

Add to `src/bug_bot/models/models.py`:

```python
class InvestigationFinding(Base):
    __tablename__ = "investigation_findings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    # category examples: "error_rate", "db_anomaly", "service_health", "metric", "log_pattern"
    finding: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    # severity at tool level: "low" | "medium" | "high" | "critical"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_investigation_findings_bug_id", "bug_id"),
        Index("idx_investigation_findings_category", "category"),
    )
```

No relationship back-pointer needed on `BugReport` for now (can be added when admin view is built).

---

## 2. Alembic Migration

New file: `alembic/versions/<new_revision>_add_investigation_findings_table.py`

- `down_revision` → `'b5d2e8f1a390'` (current latest: `add_attachments_to_bug_reports`)
- `upgrade()`: `op.create_table(...)` with all columns + `op.create_index(...)` ×2
- `downgrade()`: `op.drop_table("investigation_findings")`

Generate via `alembic revision --autogenerate -m "add_investigation_findings_table"` or write manually following the existing migration style.

---

## 3. Repository Methods

Add to `src/bug_bot/db/repository.py` inside `BugRepository`:

```python
async def save_finding(
    self,
    bug_id: str,
    category: str,
    finding: str,
    severity: str,
) -> InvestigationFinding:
    entry = InvestigationFinding(
        bug_id=bug_id, category=category, finding=finding, severity=severity
    )
    self.session.add(entry)
    await self.session.commit()
    return entry

async def get_findings_for_bug(self, bug_id: str) -> list[InvestigationFinding]:
    stmt = (
        select(InvestigationFinding)
        .where(InvestigationFinding.bug_id == bug_id)
        .order_by(InvestigationFinding.created_at)
    )
    result = await self.session.execute(stmt)
    return list(result.scalars().all())
```

Add `InvestigationFinding` to the import at the top of `repository.py`.

---

## 4. Tool Implementation in `tools.py`

### 4a. Extend the tool schema — add `bug_id`

Change the `@tool(...)` decorator for `report_finding`:

```python
@tool(
    "report_finding",
    "Log a significant finding during investigation. Use for key observations, errors found, "
    "or metrics anomalies. Always pass the bug_id of the current investigation.",
    {"bug_id": str, "category": str, "finding": str, "severity": str},
)
```

The agent already knows the bug_id (it is provided in the investigation prompt), consistent with how `close_bug` receives it.

### 4b. Add `_report_finding_sync()`

Following the exact pattern of `_close_bug_sync` — synchronous psycopg3 write to the Bug Bot DB:

```python
def _report_finding_sync(bug_id: str, category: str, finding: str, severity: str) -> str:
    if psycopg is None:
        return "Error: psycopg not installed."
    try:
        with psycopg.connect(_bugbot_conninfo(), row_factory=dict_row, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO investigation_findings (id, bug_id, category, finding, severity)
                    VALUES (gen_random_uuid(), %s, %s, %s, %s)
                    RETURNING id;
                    """,
                    (bug_id, category, finding, severity),
                )
                row = cur.fetchone()
                finding_id = row["id"] if row else "unknown"
        return f"Finding recorded (id={finding_id}): [{category}] ({severity}) {finding}"
    except Exception as e:
        return f"Error recording finding: {e}"
```

### 4c. Update `report_finding` async wrapper

```python
async def report_finding(args: dict[str, Any]) -> dict[str, Any]:
    text = await asyncio.to_thread(
        _report_finding_sync,
        args["bug_id"],
        args["category"],
        args["finding"],
        args["severity"],
    )
    return _text_result(text)
```

---

## Behaviour After Implementation

- Every `report_finding` call during investigation inserts one row into `investigation_findings`.
- The tool returns the row's UUID back to the agent (useful for debugging and prompt evals).
- Findings are ordered by `created_at`, queryable by `bug_id` or `category`.
- `get_findings_for_bug()` in the repository is ready for the admin API / admin view to consume.
- If psycopg or the DB is unavailable, the tool returns an error string (same graceful degradation pattern as all other tools) — the agent continues investigating.

---

## Verification

1. Run `alembic upgrade head` — confirm `investigation_findings` table is created with correct columns and indexes (`\d investigation_findings` in psql).
2. Start a test investigation in Slack. After the agent calls `report_finding` at least once, query the table:
   ```sql
   SELECT * FROM investigation_findings ORDER BY created_at DESC LIMIT 5;
   ```
   Confirm rows are present with correct `bug_id`, `category`, `finding`, `severity`.
3. Confirm the tool's return string includes the UUID of the inserted row (visible in Temporal activity logs).
4. Simulate psycopg failure (wrong DB URL) — confirm the agent logs an error string but does not crash.
