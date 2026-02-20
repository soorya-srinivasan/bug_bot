from dataclasses import dataclass, field
from enum import Enum


class WorkflowState(str, Enum):
    INVESTIGATING = "investigating"
    AWAITING_REPORTER = "awaiting_reporter"
    AWAITING_DEV = "awaiting_dev"
    DEV_TAKEOVER = "dev_takeover"


@dataclass
class IncomingMessage:
    sender_type: str     # "reporter" | "developer"
    sender_id: str       # Slack user ID
    conversation_id: str # UUID string of the BugConversation row already persisted to DB


@dataclass
class BugReportInput:
    bug_id: str
    channel_id: str
    thread_ts: str
    message_text: str
    reporter_user_id: str
    attachments: list[dict] = field(default_factory=list)


@dataclass
class ParsedBug:
    bug_id: str
    severity: str
    relevant_services: list[str]
    keywords: list[str]


@dataclass
class InvestigationResult:
    bug_id: str
    root_cause: str | None = None
    fix_type: str = "unknown"  # code_fix, data_fix, config_fix, needs_human, unknown
    pr_url: str | None = None
    summary: str = ""
    confidence: float = 0.0
    recommended_actions: list[str] = field(default_factory=list)
    relevant_services: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    duration_ms: int | None = None


@dataclass
class SLATrackingInput:
    bug_id: str
    severity: str
    channel_id: str
    thread_ts: str
    assigned_users: list[str] = field(default_factory=list)
