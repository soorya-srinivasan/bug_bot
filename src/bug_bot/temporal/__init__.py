from dataclasses import dataclass, field


@dataclass
class BugReportInput:
    bug_id: str
    channel_id: str
    thread_ts: str
    message_text: str
    reporter_user_id: str


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
