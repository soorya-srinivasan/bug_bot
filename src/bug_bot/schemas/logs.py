from pydantic import BaseModel


class LogQueryRequest(BaseModel):
    query: str  # free-form natural language, e.g. "errors in billing service for req-id abc123 last 2 hours"


class LogLine(BaseModel):
    timestamp: str
    stream_labels: dict[str, str]
    line: str


class LogQueryResponse(BaseModel):
    original_query: str           # echoed back
    interpreted: dict             # what Claude extracted: service, filters, time range
    matched_service: str | None   # canonical name from service_team_mapping
    logs: list[LogLine]
    total_lines: int
    query_used: str               # final LogQL that returned results
    search_strategy: str          # e.g. "exact_range", "expanded_3d", "expanded_7d"
    grafana_url: str              # Grafana Explore deep-link
    message: str
