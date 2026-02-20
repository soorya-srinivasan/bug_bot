import uuid
from datetime import datetime

from sqlalchemy import String, Text, Float, Integer, Boolean, DateTime, ForeignKey, Index
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
    attachments: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    investigation: Mapped["Investigation | None"] = relationship(back_populates="bug_report")
    escalations: Mapped[list["Escalation"]] = relationship(back_populates="bug_report")

    __table_args__ = (
        Index("idx_bug_reports_status", "status"),
        Index("idx_bug_reports_severity", "severity"),
        Index("idx_bug_reports_slack_thread_ts", "slack_thread_ts"),
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
    conversation_history: Mapped[dict | None] = mapped_column(JSONB)
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


class ServiceGroup(Base):
    __tablename__ = "service_groups"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Slack user group ID (from GET /slack/user-groups) — source of group identity.
    # Names and handles come from the Slack API; we don't duplicate them here.
    slack_group_id: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    oncall_engineer: Mapped[str | None] = mapped_column(String(20))  # Slack user ID
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    services: Mapped[list["ServiceTeamMapping"]] = relationship(back_populates="group")


class ServiceTeamMapping(Base):
    __tablename__ = "service_team_mapping"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    github_repo: Mapped[str] = mapped_column(String(200), nullable=False)
    team_slack_group: Mapped[str | None] = mapped_column(String(30))
    primary_oncall: Mapped[str | None] = mapped_column(String(20))
    tech_stack: Mapped[str] = mapped_column(String(20), nullable=False)
    service_owner: Mapped[str | None] = mapped_column(String(20))  # permanent tech owner Slack ID
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_groups.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    group: Mapped["ServiceGroup | None"] = relationship(back_populates="services")


class BugConversation(Base):
    __tablename__ = "bug_conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    channel: Mapped[str | None] = mapped_column(String(20))
    sender_type: Mapped[str] = mapped_column(String(20), nullable=False)   # reporter|developer|bot|system
    sender_id: Mapped[str | None] = mapped_column(String(50))
    message_text: Mapped[str | None] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # message_type values: bug_report | clarification_request | clarification_response |
    #   reporter_context | dev_reply | investigation_result | pr_created | resolved | status_update
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("idx_bug_conversations_bug_id", "bug_id"),
        Index("idx_bug_conversations_message_type", "message_type"),
    )


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
