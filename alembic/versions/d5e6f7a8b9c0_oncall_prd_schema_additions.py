"""oncall_prd_schema_additions

Revision ID: d5e6f7a8b9c0
Revises: c2d3e4f5a6b7
Create Date: 2026-02-21 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c2d3e4f5a6b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1.1 Team model additions ──────────────────────────────────────────────
    op.add_column("teams", sa.Column("name", sa.String(100), nullable=True))
    op.add_column("teams", sa.Column("slug", sa.String(100), nullable=True))
    op.add_column("teams", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("teams", sa.Column("slack_channel_id", sa.String(30), nullable=True))
    op.add_column("teams", sa.Column("rotation_interval", sa.String(10), server_default="weekly", nullable=False))
    op.add_column("teams", sa.Column("handoff_day", sa.Integer(), nullable=True))
    op.add_column("teams", sa.Column("handoff_time", sa.Time(), nullable=True))
    op.add_column("teams", sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False))

    # Backfill name and slug from slack_group_id
    op.execute("UPDATE teams SET name = slack_group_id WHERE name IS NULL")
    op.execute("UPDATE teams SET slug = LOWER(REPLACE(slack_group_id, ' ', '-')) WHERE slug IS NULL")

    # Now make name/slug NOT NULL and add unique constraint on slug
    op.alter_column("teams", "name", nullable=False)
    op.alter_column("teams", "slug", nullable=False)
    op.create_unique_constraint("uq_teams_slug", "teams", ["slug"])

    # ── 1.2 TeamMembership table ──────────────────────────────────────────────
    op.create_table(
        "team_memberships",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("team_id", UUID(as_uuid=True), sa.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slack_user_id", sa.String(20), nullable=False),
        sa.Column("team_role", sa.String(10), server_default="member", nullable=False),
        sa.Column("is_eligible_for_oncall", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("weight", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("team_id", "slack_user_id", name="uq_team_memberships_team_user"),
    )
    op.create_index("idx_team_memberships_team_id", "team_memberships", ["team_id"])

    # ── 1.3 ServiceTeamMapping additions ──────────────────────────────────────
    op.add_column("service_team_mapping", sa.Column("repository_url", sa.String(500), nullable=True))
    op.add_column("service_team_mapping", sa.Column("environment", sa.String(50), nullable=True))
    op.add_column("service_team_mapping", sa.Column("tier", sa.String(20), nullable=True))
    op.add_column("service_team_mapping", sa.Column("metadata", JSONB(), nullable=True))
    op.add_column(
        "service_team_mapping",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )

    # Backfill repository_url from github_repo
    op.execute(
        "UPDATE service_team_mapping SET repository_url = github_repo WHERE repository_url IS NULL AND github_repo IS NOT NULL"
    )

    # ── 1.4 Schedule & Override additions ─────────────────────────────────────
    # oncall_schedules: add origin
    op.add_column(
        "oncall_schedules",
        sa.Column("origin", sa.String(10), server_default="manual", nullable=False),
    )

    # oncall_overrides: add status, requested_by, approved_by
    op.add_column(
        "oncall_overrides",
        sa.Column("status", sa.String(20), server_default="approved", nullable=False),
    )
    op.add_column("oncall_overrides", sa.Column("requested_by", sa.String(20), nullable=True))
    op.add_column("oncall_overrides", sa.Column("approved_by", sa.String(20), nullable=True))

    # Backfill: existing overrides get status='approved' and requested_by=created_by
    op.execute("UPDATE oncall_overrides SET status = 'approved' WHERE status IS NULL OR status = ''")
    op.execute("UPDATE oncall_overrides SET requested_by = created_by WHERE requested_by IS NULL")

    # ── 1.5 OnCallAuditLog table ─────────────────────────────────────────────
    op.create_table(
        "oncall_audit_logs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("team_id", UUID(as_uuid=True), sa.ForeignKey("teams.id"), nullable=True),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("actor_type", sa.String(10), server_default="user", nullable=False),
        sa.Column("actor_id", sa.String(20), nullable=True),
        sa.Column("changes", JSONB(), nullable=True),
        sa.Column("metadata", JSONB(), nullable=True),
        # Legacy compat columns
        sa.Column("engineer_slack_id", sa.String(20), nullable=True),
        sa.Column("previous_engineer_slack_id", sa.String(20), nullable=True),
        sa.Column("change_type", sa.String(20), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("effective_date", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("idx_oncall_audit_logs_entity", "oncall_audit_logs", ["entity_type", "entity_id"])
    op.create_index("idx_oncall_audit_logs_team_id", "oncall_audit_logs", ["team_id"])
    op.create_index("idx_oncall_audit_logs_action", "oncall_audit_logs", ["action"])
    op.create_index("idx_oncall_audit_logs_created_at", "oncall_audit_logs", ["created_at"])


def downgrade() -> None:
    # Drop oncall_audit_logs
    op.drop_index("idx_oncall_audit_logs_created_at")
    op.drop_index("idx_oncall_audit_logs_action")
    op.drop_index("idx_oncall_audit_logs_team_id")
    op.drop_index("idx_oncall_audit_logs_entity")
    op.drop_table("oncall_audit_logs")

    # Remove override additions
    op.drop_column("oncall_overrides", "approved_by")
    op.drop_column("oncall_overrides", "requested_by")
    op.drop_column("oncall_overrides", "status")

    # Remove schedule origin
    op.drop_column("oncall_schedules", "origin")

    # Remove service_team_mapping additions
    op.drop_column("service_team_mapping", "is_active")
    op.drop_column("service_team_mapping", "metadata")
    op.drop_column("service_team_mapping", "tier")
    op.drop_column("service_team_mapping", "environment")
    op.drop_column("service_team_mapping", "repository_url")

    # Drop team_memberships
    op.drop_index("idx_team_memberships_team_id")
    op.drop_table("team_memberships")

    # Remove team additions
    op.drop_constraint("uq_teams_slug", "teams", type_="unique")
    op.drop_column("teams", "is_active")
    op.drop_column("teams", "handoff_time")
    op.drop_column("teams", "handoff_day")
    op.drop_column("teams", "rotation_interval")
    op.drop_column("teams", "slack_channel_id")
    op.drop_column("teams", "description")
    op.drop_column("teams", "slug")
    op.drop_column("teams", "name")
