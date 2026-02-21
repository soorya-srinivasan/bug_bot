import uuid
from datetime import datetime, date, time

from pgvector.sqlalchemy import Vector
from sqlalchemy import String, Text, Float, Integer, Boolean, DateTime, Date, Time, ForeignKey, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class BugReport(Base):
    __tablename__ = "bug_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    slack_channel_id: Mapped[str] = mapped_column(String(20), nullable=False)
    slack_thread_ts: Mapped[str] = mapped_column(String(30), nullable=False)
    reporter_user_id: Mapped[str] = mapped_column(String(20), nullable=False)
    original_message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(5), nullable=False, default="P3")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(100))
    assignee_user_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    attachments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    closure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    fix_provided: Mapped[str | None] = mapped_column(Text, nullable=True)

    investigation: Mapped["Investigation | None"] = relationship(back_populates="bug_report")
    escalations: Mapped[list["Escalation"]] = relationship(back_populates="bug_report")

    __table_args__ = (
        Index("idx_bug_reports_status", "status"),
        Index("idx_bug_reports_severity", "severity"),
        Index("idx_bug_reports_slack_thread_ts", "slack_thread_ts"),
        Index("idx_bug_reports_resolution_type", "resolution_type"),
    )


class Investigation(Base):
    __tablename__ = "investigations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    root_cause: Mapped[str | None] = mapped_column(Text)
    fix_type: Mapped[str] = mapped_column(String(20), nullable=False)
    pr_url: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    relevant_services: Mapped[dict] = mapped_column(JSONB, default=list)
    recommended_actions: Mapped[dict] = mapped_column(JSONB, default=list)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    summary_thread_ts: Mapped[str | None] = mapped_column(String(30))
    claude_session_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bug_report: Mapped["BugReport"] = relationship(back_populates="investigation")

    __table_args__ = (
        Index("idx_investigations_summary_thread_ts", "summary_thread_ts"),
    )


class SLAConfig(Base):
    __tablename__ = "sla_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    severity: Mapped[str] = mapped_column(String(5), unique=True, nullable=False)
    acknowledgement_target_min: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_target_min: Mapped[int] = mapped_column(Integer, nullable=False)
    follow_up_interval_min: Mapped[int] = mapped_column(Integer, nullable=False)
    escalation_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    escalation_contacts: Mapped[dict] = mapped_column(JSONB, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    escalated_to: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bug_report: Mapped["BugReport"] = relationship(back_populates="escalations")


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slack_group_id: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    slack_channel_id: Mapped[str | None] = mapped_column(String(30), nullable=True)
    oncall_engineer: Mapped[str | None] = mapped_column(String(20))
    # Rotation configuration
    rotation_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    rotation_type: Mapped[str | None] = mapped_column(String(20))  # 'round_robin' | 'custom_order' | 'weighted'
    rotation_order: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    rotation_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    current_rotation_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rotation_interval: Mapped[str] = mapped_column(String(10), default="weekly", nullable=False)  # 'daily' | 'weekly' | 'biweekly'
    handoff_day: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0=Mon, 6=Sun
    handoff_time: Mapped[time | None] = mapped_column(Time, nullable=True)  # UTC
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    services: Mapped[list["ServiceTeamMapping"]] = relationship(back_populates="team")
    schedules: Mapped[list["OnCallSchedule"]] = relationship(back_populates="team", cascade="all, delete-orphan")
    history: Mapped[list["OnCallHistory"]] = relationship(back_populates="team", cascade="all, delete-orphan")
    overrides: Mapped[list["OnCallOverride"]] = relationship(back_populates="team", cascade="all, delete-orphan")
    memberships: Mapped[list["TeamMembership"]] = relationship(back_populates="team", cascade="all, delete-orphan")


class TeamMembership(Base):
    __tablename__ = "team_memberships"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    slack_user_id: Mapped[str] = mapped_column(String(20), nullable=False)
    team_role: Mapped[str] = mapped_column(String(10), default="member", nullable=False)  # 'lead' | 'member'
    is_eligible_for_oncall: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    team: Mapped["Team"] = relationship(back_populates="memberships")

    __table_args__ = (
        UniqueConstraint("team_id", "slack_user_id", name="uq_team_memberships_team_user"),
        Index("idx_team_memberships_team_id", "team_id"),
    )


class ServiceTeamMapping(Base):
    __tablename__ = "service_team_mapping"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    github_repo: Mapped[str] = mapped_column(String(200), nullable=False)
    team_slack_group: Mapped[str | None] = mapped_column(String(30))
    primary_oncall: Mapped[str | None] = mapped_column(String(20))
    tech_stack: Mapped[str] = mapped_column(String(20), nullable=False)
    service_owner: Mapped[str | None] = mapped_column(String(20))
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    repository_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    environment: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tier: Mapped[str | None] = mapped_column(String(20), nullable=True)  # 'critical' | 'standard' | 'low'
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    team: Mapped["Team | None"] = relationship(back_populates="services")


class BugConversation(Base):
    __tablename__ = "bug_conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(20))
    sender_type: Mapped[str] = mapped_column(String(20), nullable=False)   # reporter|developer|bot|system
    sender_id: Mapped[str | None] = mapped_column(String(50))
    message_text: Mapped[str | None] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(30), nullable=False)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_bug_conversations_bug_id", "bug_id"),
        Index("idx_bug_conversations_message_type", "message_type"),
    )


class BugAuditLog(Base):
    __tablename__ = "bug_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    performed_by: Mapped[str | None] = mapped_column(String(50), nullable=True)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_bug_audit_logs_bug_id", "bug_id"),
        Index("idx_bug_audit_logs_action", "action"),
    )


class InvestigationFinding(Base):
    __tablename__ = "investigation_findings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    finding: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_investigation_findings_bug_id", "bug_id"),
        Index("idx_investigation_findings_category", "category"),
    )


class InvestigationMessage(Base):
    __tablename__ = "investigation_messages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    investigation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigations.id"), nullable=True
    )
    followup_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("investigation_followups.id"), nullable=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    message_type: Mapped[str] = mapped_column(String(30), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_investigation_messages_bug_id", "bug_id"),
        Index("idx_investigation_messages_investigation_id", "investigation_id"),
        Index("idx_investigation_messages_followup_id", "followup_id"),
    )


class InvestigationFollowup(Base):
    __tablename__ = "investigation_followups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    trigger_state: Mapped[str] = mapped_column(String(20), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    fix_type: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    root_cause: Mapped[str | None] = mapped_column(Text)
    pr_url: Mapped[str | None] = mapped_column(String(500))
    recommended_actions: Mapped[dict] = mapped_column(JSONB, default=list)
    relevant_services: Mapped[dict] = mapped_column(JSONB, default=list)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_investigation_followups_bug_id", "bug_id"),
    )


class OnCallSchedule(Base):
    __tablename__ = "oncall_schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    engineer_slack_id: Mapped[str] = mapped_column(String(20), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    schedule_type: Mapped[str] = mapped_column(String(10), nullable=False)  # 'weekly' | 'daily'
    days_of_week: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    origin: Mapped[str] = mapped_column(String(10), default="manual", nullable=False)  # 'auto' | 'manual'
    created_by: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    team: Mapped["Team"] = relationship(back_populates="schedules")

    __table_args__ = (
        Index("idx_oncall_schedules_team_start", "team_id", "start_date"),
        Index("idx_oncall_schedules_team_end", "team_id", "end_date"),
    )


class OnCallOverride(Base):
    __tablename__ = "oncall_overrides"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    override_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    substitute_engineer_slack_id: Mapped[str] = mapped_column(String(20), nullable=False)
    original_engineer_slack_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="approved", nullable=False)  # 'pending' | 'approved' | 'rejected' | 'cancelled'
    requested_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_by: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    team: Mapped["Team"] = relationship(back_populates="overrides")

    __table_args__ = (
        Index("idx_oncall_overrides_team_date", "team_id", "override_date"),
    )


class OnCallHistory(Base):
    __tablename__ = "oncall_history"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    engineer_slack_id: Mapped[str] = mapped_column(String(20), nullable=False)
    previous_engineer_slack_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    change_type: Mapped[str] = mapped_column(String(20), nullable=False)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    changed_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    team: Mapped["Team"] = relationship(back_populates="history")

    __table_args__ = (
        Index("idx_oncall_history_team_effective", "team_id", "effective_date"),
        Index("idx_oncall_history_team_created", "team_id", "created_at"),
    )


class OnCallAuditLog(Base):
    __tablename__ = "oncall_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id"), nullable=True
    )
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)  # 'team'|'service'|'schedule'|'override'|'rotation_config'|'team_membership'
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)  # 'created'|'updated'|'deleted'|'rotation_triggered'|'override_approved'|...
    actor_type: Mapped[str] = mapped_column(String(10), default="user", nullable=False)  # 'user'|'system'
    actor_id: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Slack user ID
    changes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {"field": {"old": X, "new": Y}}
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    # Legacy compat columns
    engineer_slack_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    previous_engineer_slack_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    change_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    change_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_oncall_audit_logs_entity", "entity_type", "entity_id"),
        Index("idx_oncall_audit_logs_team_id", "team_id"),
        Index("idx_oncall_audit_logs_action", "action"),
        Index("idx_oncall_audit_logs_created_at", "created_at"),
    )


class RagDocument(Base):
    __tablename__ = "rag_documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    embedding = mapped_column(Vector(384), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        Index("idx_rag_documents_source", "source_type", "source_id"),
    )
