"""add bug_audit_logs table

Revision ID: e9f1a2b3c4d7
Revises: f8a2b3c4d5e6
Create Date: 2026-02-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision = "e9f1a2b3c4d7"
down_revision = "7d4399b0b6d6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bug_audit_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("bug_id", sa.String(50), sa.ForeignKey("bug_reports.bug_id"), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("performed_by", sa.String(50), nullable=True),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("metadata", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("idx_bug_audit_logs_bug_id", "bug_audit_logs", ["bug_id"])
    op.create_index("idx_bug_audit_logs_action", "bug_audit_logs", ["action"])


def downgrade() -> None:
    op.drop_index("idx_bug_audit_logs_action", table_name="bug_audit_logs")
    op.drop_index("idx_bug_audit_logs_bug_id", table_name="bug_audit_logs")
    op.drop_table("bug_audit_logs")
