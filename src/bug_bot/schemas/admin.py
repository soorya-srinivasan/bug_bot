from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, NonNegativeInt


Severity = Literal["P1", "P2", "P3", "P4"]
Status = Literal["new", "triaged", "investigating", "awaiting_dev", "escalated", "resolved"]


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
    investigation_summary: InvestigationSummary | None = None


class PaginatedBugs(BaseModel):
    items: list[BugListItem]
    total: NonNegativeInt
    page: int
    page_size: int


class BugUpdate(BaseModel):
    severity: Severity | None = None
    status: Status | None = None


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
    conversation_history: Any = None
    summary_thread_ts: str | None = None
    created_at: datetime


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


class ServiceTeamMappingBase(BaseModel):
    service_name: str
    github_repo: str
    team_slack_group: str | None = None
    primary_oncall: str | None = None
    tech_stack: str


class ServiceTeamMappingCreate(ServiceTeamMappingBase):
    pass


class ServiceTeamMappingUpdate(BaseModel):
    service_name: str | None = None
    github_repo: str | None = None
    team_slack_group: str | None = None
    primary_oncall: str | None = None
    tech_stack: str | None = None


class ServiceTeamMappingResponse(ServiceTeamMappingBase):
    id: str
    created_at: datetime


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

