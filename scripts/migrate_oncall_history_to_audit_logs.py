"""One-time migration: copy oncall_history rows into oncall_audit_logs.

Run manually:
    python scripts/migrate_oncall_history_to_audit_logs.py

This script is idempotent — it skips rows whose (change_type, team_id,
effective_date, engineer_slack_id, created_at) tuple already exists in
the audit log table.
"""

import asyncio
import logging
import uuid

from sqlalchemy import select, text, func

from bug_bot.db.session import async_session
from bug_bot.models.models import OnCallHistory, OnCallAuditLog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Mapping: oncall_history.change_type -> (action, entity_type)
CHANGE_TYPE_MAP = {
    "manual": ("updated", "team"),
    "auto_rotation": ("rotation_triggered", "team"),
    "schedule_created": ("created", "schedule"),
    "schedule_updated": ("updated", "schedule"),
    "schedule_deleted": ("deleted", "schedule"),
    "override_created": ("created", "override"),
    "override_deleted": ("deleted", "override"),
}


async def migrate():
    batch_size = 500
    total_migrated = 0
    total_skipped = 0
    offset = 0

    async with async_session() as session:
        # Count total rows
        count_result = await session.execute(select(func.count()).select_from(OnCallHistory))
        total_rows = count_result.scalar() or 0
        logger.info("Found %d oncall_history rows to migrate", total_rows)

        while True:
            result = await session.execute(
                select(OnCallHistory)
                .order_by(OnCallHistory.created_at)
                .offset(offset)
                .limit(batch_size)
            )
            rows = result.scalars().all()
            if not rows:
                break

            for h in rows:
                action, entity_type = CHANGE_TYPE_MAP.get(
                    h.change_type, ("unknown", "unknown")
                )
                actor_type = "user" if h.changed_by else "system"

                # Check for existing entry (idempotency)
                existing = await session.execute(
                    select(OnCallAuditLog.id).where(
                        OnCallAuditLog.team_id == h.team_id,
                        OnCallAuditLog.change_type == h.change_type,
                        OnCallAuditLog.effective_date == h.effective_date,
                        OnCallAuditLog.engineer_slack_id == h.engineer_slack_id,
                        OnCallAuditLog.created_at == h.created_at,
                    ).limit(1)
                )
                if existing.scalar() is not None:
                    total_skipped += 1
                    continue

                audit_entry = OnCallAuditLog(
                    id=uuid.uuid4(),
                    team_id=h.team_id,
                    entity_type=entity_type,
                    entity_id=h.team_id,
                    action=action,
                    actor_type=actor_type,
                    actor_id=h.changed_by,
                    # Legacy compat columns
                    engineer_slack_id=h.engineer_slack_id,
                    previous_engineer_slack_id=h.previous_engineer_slack_id,
                    change_type=h.change_type,
                    change_reason=h.change_reason,
                    effective_date=h.effective_date,
                    created_at=h.created_at,
                )
                session.add(audit_entry)
                total_migrated += 1

            await session.commit()
            offset += batch_size
            logger.info(
                "Progress: %d/%d migrated, %d skipped",
                total_migrated, total_rows, total_skipped,
            )

    logger.info(
        "Migration complete: %d migrated, %d skipped, %d total source rows",
        total_migrated, total_skipped, total_rows,
    )


if __name__ == "__main__":
    asyncio.run(migrate())
