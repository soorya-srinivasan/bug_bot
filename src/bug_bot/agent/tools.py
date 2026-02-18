from typing import Any

from claude_agent_sdk import tool, create_sdk_mcp_server

from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository


@tool(
    "lookup_service_owner",
    "Look up the team, on-call engineer, and GitHub repo for a given service name.",
    {"service_name": str},
)
async def lookup_service_owner(args: dict[str, Any]) -> dict[str, Any]:
    async with async_session() as session:
        repo = BugRepository(session)
        mapping = await repo.get_service_mapping(args["service_name"])

    if mapping is None:
        return {
            "content": [{"type": "text", "text": f"No mapping found for service: {args['service_name']}"}]
        }

    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Service: {mapping.service_name}\n"
                    f"GitHub repo: {mapping.github_repo}\n"
                    f"Team Slack group: {mapping.team_slack_group or 'N/A'}\n"
                    f"Primary on-call: {mapping.primary_oncall or 'N/A'}\n"
                    f"Tech stack: {mapping.tech_stack}"
                ),
            }
        ]
    }


@tool(
    "report_finding",
    "Log a significant finding during investigation. Use for key observations, errors found, or metrics anomalies.",
    {"category": str, "finding": str, "severity": str},
)
async def report_finding(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": f"Finding recorded: [{args['category']}] ({args['severity']}) {args['finding']}",
            }
        ]
    }


def build_custom_tools_server():
    """Create an in-process MCP server with Bug Bot custom tools."""
    return create_sdk_mcp_server(
        name="bugbot_tools",
        version="1.0.0",
        tools=[lookup_service_owner, report_finding],
    )
