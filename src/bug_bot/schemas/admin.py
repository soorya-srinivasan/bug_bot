from datetime import datetime, date
from typing import Literal

from pydantic import BaseModel, Field, NonNegativeInt


Severity = Literal["P1", "P2", "P3", "P4"]
Status = Literal["new", "triaged", "investigating", "awaiting_dev", "escalated", "resolved", "dev_takeover", "pending_verification"]


class PaginationParams(BaseModel):
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)


class BugFilters(PaginationParams):
    status: Status | None = None
    severity: Severity | None = None
    service: str | None = None
    from_date: datetime | None = None
    to_date: datetime | None = None
    sort: str = "-created_at"


class InvestigationSummary(BaseModel):
    summary: str
    fix_type: str
    confidence: float | None = None


class TaggedOnEntry(BaseModel):
    oncall_engineer: str | None = None
    service_owner: str | None = None
    slack_group_id: str | None = None


class BugListItem(BaseModel):
    id: str
    bug_id: str
    slack_channel_id: str
    slack_thread_ts: str
    slack_message_url: str
    reporter_user_id: str
    original_message: str
    severity: Severity
    status: Status
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    assignee_user_id: str | None = None
    investigation_summary: InvestigationSummary | None = None
    tagged_on: list[TaggedOnEntry] = []
    current_on_call: list[TaggedOnEntry] = []


class PaginatedBugs(BaseModel):
    items: list[BugListItem]
    total: NonNegativeInt
    page: int
    page_size: int


class BugUpdate(BaseModel):
    severity: Severity | None = None
    status: Status | None = None


class InvestigationMessageResponse(BaseModel):
    id: str
    sequence: int
    message_type: str
    content: str | None = None
    created_at: datetime


class InvestigationResponse(BaseModel):
    bug_id: str
    root_cause: str | None = None
    fix_type: str
    pr_url: str | None = None
    summary: str
    confidence: float
    relevant_services: list[str] = []
    recommended_actions: list[str] = []
    cost_usd: float | None = None
    duration_ms: int | None = None
    messages: list[InvestigationMessageResponse] = []
    followups: list["InvestigationFollowupResponse"] = []
    summary_thread_ts: str | None = None
    created_at: datetime


class InvestigationFollowupResponse(BaseModel):
    id: str
    bug_id: str
    trigger_state: str
    action: str
    fix_type: str
    summary: str
    confidence: float
    root_cause: str | None = None
    pr_url: str | None = None
    recommended_actions: list[str] = []
    relevant_services: list[str] = []
    cost_usd: float | None = None
    duration_ms: int | None = None
    messages: list[InvestigationMessageResponse] = []
    created_at: datetime


class InvestigationFollowupListResponse(BaseModel):
    items: list[InvestigationFollowupResponse]


class EscalationCreate(BaseModel):
    escalation_level: int = Field(..., ge=1)
    escalated_to: list[str] = Field(..., min_length=1)
    reason: str | None = None


class EscalationResponse(BaseModel):
    id: str
    bug_id: str
    escalation_level: int
    escalated_to: list[str]
    reason: str | None = None
    created_at: datetime


class SLAConfigBase(BaseModel):
    severity: Severity
    acknowledgement_target_min: int = Field(..., gt=0)
    resolution_target_min: int = Field(..., gt=0)
    follow_up_interval_min: int = Field(..., gt=0)
    escalation_threshold: int = Field(..., gt=0)
    escalation_contacts: list[str] = Field(default_factory=list)
    is_active: bool = True


class SLAConfigCreate(SLAConfigBase):
    pass


class SLAConfigUpdate(BaseModel):
    acknowledgement_target_min: int | None = Field(default=None, gt=0)
    resolution_target_min: int | None = Field(default=None, gt=0)
    follow_up_interval_min: int | None = Field(default=None, gt=0)
    escalation_threshold: int | None = Field(default=None, gt=0)
    escalation_contacts: list[str] | None = None
    is_active: bool | None = None


class SLAConfigResponse(SLAConfigBase):
    id: str
    created_at: datetime
    updated_at: datetime


class SLAConfigListResponse(BaseModel):
    items: list[SLAConfigResponse]


class TeamBase(BaseModel):
    slack_group_id: str          # Slack user group ID from GET /slack/user-groups
    oncall_engineer: str | None = None  # Slack user ID — pick from GET /slack/user-groups/users


class TeamCreate(TeamBase):
    pass


class TeamUpdate(BaseModel):
    oncall_engineer: str | None = None  # only oncall_engineer is mutable after creation


class TeamRotationConfigUpdate(BaseModel):
    rotation_enabled: bool | None = None
    rotation_type: Literal["round_robin", "custom_order"] | None = None
    rotation_order: list[str] | None = None
    rotation_start_date: date | None = None


class TeamRotationConfig(BaseModel):
    rotation_enabled: bool = False
    rotation_type: Literal["round_robin", "custom_order"] | None = None
    rotation_order: list[str] | None = None  # array of Slack user IDs for custom_order
    rotation_start_date: date | None = None
    current_rotation_index: int | None = None


class TeamResponse(TeamBase):
    id: str
    rotation_enabled: bool = False
    rotation_type: Literal["round_robin", "custom_order"] | None = None
    rotation_order: list[str] | None = None
    rotation_start_date: date | None = None
    current_rotation_index: int | None = None
    created_at: datetime
    updated_at: datetime


class PaginatedTeams(BaseModel):
    items: list[TeamResponse]
    total: NonNegativeInt
    page: int
    page_size: int


class TeamSummary(BaseModel):
    id: str
    slack_group_id: str       # Slack user group ID — look up name/handle via GET /slack/user-groups
    oncall_engineer: str | None


class ServiceTeamMappingBase(BaseModel):
    service_name: str
    github_repo: str
    team_slack_group: str
    primary_oncall: str
    tech_stack: str
    service_owner: str | None = None
    team_id: str | None = None


class ServiceTeamMappingCreate(ServiceTeamMappingBase):
    pass


class ServiceTeamMappingUpdate(BaseModel):
    service_name: str | None = None
    github_repo: str | None = None
    team_slack_group: str | None = None
    primary_oncall: str | None = None
    tech_stack: str | None = None
    service_owner: str | None = None
    team_id: str | None = None


class ServiceTeamMappingResponse(ServiceTeamMappingBase):
    id: str
    created_at: datetime
    team: TeamSummary | None = None  # populated when team_id is set


class PaginatedServiceTeamMappings(BaseModel):
    items: list[ServiceTeamMappingResponse]
    total: NonNegativeInt
    page: int
    page_size: int


# --- Slack user groups (admin) ---


class SlackUserGroupListItem(BaseModel):
    """A Slack user group (mention group) summary."""

    id: str
    name: str
    handle: str
    description: str | None = None
    user_count: int | None = None
    date_create: int | None = None


class SlackUserGroupListResponse(BaseModel):
    items: list[SlackUserGroupListItem]


class SlackUserGroupUsersRequest(BaseModel):
    """Request body to list users in a Slack user group."""

    usergroup_id: str = Field(..., description="Slack user group ID (e.g. S01234567)")

    model_config = {
        "json_schema_extra": {
            "examples": [{"usergroup_id": "S01234567"}],
        },
    }


class SlackUserDetail(BaseModel):
    """Slack user info when include_user_details=true."""

    id: str
    name: str | None = None
    real_name: str | None = None
    display_name: str | None = None
    is_bot: bool = False
    deleted: bool = False


class SlackUserGroupUsersResponse(BaseModel):
    """Users in a Slack user group."""

    usergroup_id: str
    user_ids: list[str]
    users: list[SlackUserDetail] | None = None


class SlackUsersLookupResponse(BaseModel):
    """Batch user lookup result keyed by Slack user ID."""

    users: dict[str, SlackUserDetail]
    team_id: str | None = None


# --- On-Call Scheduling ---


class OnCallScheduleBase(BaseModel):
    engineer_slack_id: str
    start_date: date
    end_date: date
    schedule_type: Literal["weekly", "daily"]
    days_of_week: list[int] | None = None  # array of day numbers [0-6] for daily schedules (0=Monday)


class OnCallScheduleCreate(OnCallScheduleBase):
    pass


class OnCallScheduleUpdate(BaseModel):
    engineer_slack_id: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    schedule_type: Literal["weekly", "daily"] | None = None
    days_of_week: list[int] | None = None


class OnCallScheduleResponse(OnCallScheduleBase):
    id: str
    team_id: str
    created_by: str
    created_at: datetime
    updated_at: datetime


class PaginatedOnCallSchedules(BaseModel):
    items: list[OnCallScheduleResponse]
    total: NonNegativeInt
    page: int
    page_size: int


class OnCallHistoryResponse(BaseModel):
    id: str
    team_id: str
    engineer_slack_id: str
    previous_engineer_slack_id: str | None = None
    change_type: Literal["manual", "auto_rotation", "schedule_created", "schedule_updated", "schedule_deleted"]
    change_reason: str | None = None
    effective_date: date
    changed_by: str | None = None
    created_at: datetime


class PaginatedOnCallHistory(BaseModel):
    items: list[OnCallHistoryResponse]
    total: NonNegativeInt
    page: int
    page_size: int


class CurrentOnCallResponse(BaseModel):
    engineer_slack_id: str | None
    effective_date: date | None
    source: Literal["schedule", "rotation", "manual"] | None
    schedule_id: str | None = None


# --- Bug Conversations & Findings ---


class BugConversationResponse(BaseModel):
    id: str
    bug_id: str
    channel: str | None = None
    sender_type: str
    sender_id: str | None = None
    message_text: str | None = None
    message_type: str
    metadata: dict | None = None
    created_at: datetime


class BugConversationListResponse(BaseModel):
    items: list[BugConversationResponse]


class InvestigationFindingResponse(BaseModel):
    id: str
    bug_id: str
    category: str
    finding: str
    severity: str
    created_at: datetime


class InvestigationFindingListResponse(BaseModel):
    items: list[InvestigationFindingResponse]


class NudgeResponse(BaseModel):
    """Result of sending a Slack nudge to tagged on-call engineers."""

    bug_id: str
    nudged_users: list[str]
    failed_users: list[str]
    message: str


# --- Dashboard Analytics ---


class StatusCount(BaseModel):
    status: str
    count: int


class SeverityCount(BaseModel):
    severity: str
    count: int


class DailyBugCount(BaseModel):
    date: date
    created: int = 0
    resolved: int = 0


class SeverityResolution(BaseModel):
    severity: str
    avg_hours: float


class FixTypeCount(BaseModel):
    fix_type: str
    count: int


class ServiceBugCount(BaseModel):
    service: str
    count: int


class CategoryCount(BaseModel):
    category: str
    count: int


class FindingSeverityCount(BaseModel):
    severity: str
    count: int


class RecentBugItem(BaseModel):
    bug_id: str
    severity: str
    status: str
    original_message: str
    created_at: datetime


class DashboardResponse(BaseModel):
    total_bugs: int
    open_bugs: int
    resolved_bugs: int
    avg_resolution_hours: float | None = None
    escalation_rate: float = 0.0
    avg_confidence: float | None = None
    total_investigation_cost_usd: float = 0.0
    avg_investigation_duration_ms: float | None = None

    bugs_by_status: list[StatusCount] = []
    bugs_by_severity: list[SeverityCount] = []
    bug_trend: list[DailyBugCount] = []
    avg_resolution_by_severity: list[SeverityResolution] = []
    fix_type_distribution: list[FixTypeCount] = []
    top_services: list[ServiceBugCount] = []
    findings_by_category: list[CategoryCount] = []
    findings_by_severity: list[FindingSeverityCount] = []
    recent_bugs: list[RecentBugItem] = []

