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


class ServiceTeamMapping(Base):
    __tablename__ = "service_team_mapping"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    github_repo: Mapped[str] = mapped_column(String(200), nullable=False)
    team_slack_group: Mapped[str | None] = mapped_column(String(30))
    primary_oncall: Mapped[str | None] = mapped_column(String(20))
    tech_stack: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
