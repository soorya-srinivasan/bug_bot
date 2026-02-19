import os
import time

from claude_agent_sdk import (
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ResultMessage,
)

from bug_bot.agent.mcp_config import build_mcp_servers
from bug_bot.agent.tools import build_custom_tools_server
from bug_bot.agent.prompts import build_investigation_prompt
from bug_bot.config import settings


async def run_investigation(
    bug_id: str,
    description: str,
    severity: str,
    relevant_services: list[str],
) -> dict:
    """Run a Claude Agent SDK investigation for the given bug."""
    workspace = "/tmp/bugbot-workspace"
    os.makedirs(workspace, exist_ok=True)

    start_time = time.time()

    mcp_servers = build_mcp_servers()
    custom_server = build_custom_tools_server()

    # Add custom tools server
    mcp_servers["bugbot_tools"] = custom_server

    # Build allowed tools list — allow all MCP tools + file tools
    allowed_tools = ["Read", "Glob", "Grep", "Bash", "Write", "Edit"]
    for server_name in mcp_servers:
        allowed_tools.append(f"mcp__{server_name}__*")

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-5-20250929",
        permission_mode="bypassPermissions",
        max_turns=50,
        cwd="/tmp/bugbot-workspace",
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
        system_prompt=(
            "You are Bug Bot, an automated bug investigation agent for ShopTech. "
            "You have access to Grafana, New Relic, GitHub, Git, PostgreSQL, and MySQL "
            "via MCP servers. Your goal is to investigate bug reports, identify root causes, "
            "and create fixes when possible.\n\n"
            "For .NET services (OXO.APIs): check Grafana dashboards, look for common C#/.NET issues.\n"
            "For Ruby/Rails services (vconnect): check New Relic APM, look for Rails-specific issues.\n\n"
            "IMPORTANT: When creating code fixes:\n"
            "- Clone repos to /tmp/bugbot-workspace/\n"
            "- Branch naming: <bug_id>-<short-desc>\n"
            "- Commit message: fix(<service>): <desc> [<bug_id>]\n"
            "- Create a PR with the bug ID in the title\n"
            "- Keep changes minimal — only fix the reported issue\n"
            "- Never push to main/master directly\n\n"
            "Always provide your findings in a structured format at the end."
        ),
        output_format={
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "root_cause": {"type": ["string", "null"]},
                    "fix_type": {
                        "type": "string",
                        "enum": ["code_fix", "data_fix", "config_fix", "needs_human", "unknown"],
                    },
                    "pr_url": {"type": ["string", "null"]},
                    "summary": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "recommended_actions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "relevant_services": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["fix_type", "summary", "confidence"],
            },
        },
    )

    prompt = build_investigation_prompt(bug_id, description, severity, relevant_services)

    result_data = None
    total_cost = None
    session_id = None
    conversation_history = []

    async for message in query(prompt=prompt, options=options):
        msg_record = {"type": type(message).__name__}
        if isinstance(message, AssistantMessage):
            text_parts = []
            for block in message.content:
                if isinstance(block, TextBlock):
                    text_parts.append(block.text)
            msg_record["text"] = "\n".join(text_parts)
        elif isinstance(message, ResultMessage):
            msg_record["structured_output"] = message.structured_output
            result_data = message.structured_output
            total_cost = getattr(message, "total_cost_usd", None)
            session_id = getattr(message, "session_id", None)
        conversation_history.append(msg_record)

    elapsed_ms = int((time.time() - start_time) * 1000)

    if result_data is None:
        result_data = {
            "fix_type": "unknown",
            "summary": "Agent investigation did not produce structured results.",
            "confidence": 0.0,
            "recommended_actions": ["Manual investigation required"],
            "relevant_services": relevant_services,
        }

    result_data["cost_usd"] = total_cost
    result_data["duration_ms"] = elapsed_ms
    result_data.setdefault("relevant_services", relevant_services)
    result_data.setdefault("recommended_actions", [])
    result_data["conversation_history"] = conversation_history
    result_data["claude_session_id"] = session_id

    return result_data


async def run_followup(
    bug_id: str,
    dev_message: str,
    reply_type: str,
    claude_session_id: str | None = None,
) -> dict:
    """Run a follow-up investigation by resuming the original Claude session."""
    workspace = "/tmp/bugbot-workspace"
    os.makedirs(workspace, exist_ok=True)

    start_time = time.time()

    mcp_servers = build_mcp_servers()
    custom_server = build_custom_tools_server()
    mcp_servers["bugbot_tools"] = custom_server

    allowed_tools = ["Read", "Glob", "Grep", "Bash", "Write", "Edit"]
    for server_name in mcp_servers:
        allowed_tools.append(f"mcp__{server_name}__*")

    prompt = (
        f"A developer has replied to your investigation of bug {bug_id} ({reply_type}):\n\n"
        f"{dev_message}\n\n"
        f"{'Attempt to create a code fix and open a PR.' if reply_type == 'approve' else 'Use this additional context to refine your analysis and provide updated findings.'}"
    )

    options = ClaudeAgentOptions(
        model="claude-sonnet-4-5-20250929",
        permission_mode="bypassPermissions",
        max_turns=50,
        cwd="/tmp/bugbot-workspace",
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
        resume=claude_session_id,  # Resume the exact session — full context preserved
        output_format={
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {
                    "root_cause": {"type": ["string", "null"]},
                    "fix_type": {
                        "type": "string",
                        "enum": ["code_fix", "data_fix", "config_fix", "needs_human", "unknown"],
                    },
                    "pr_url": {"type": ["string", "null"]},
                    "summary": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "recommended_actions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "relevant_services": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": ["fix_type", "summary", "confidence"],
            },
        },
    )

    result_data = None
    total_cost = None
    new_session_id = None

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result_data = message.structured_output
            total_cost = getattr(message, "total_cost_usd", None)
            new_session_id = getattr(message, "session_id", None)

    elapsed_ms = int((time.time() - start_time) * 1000)

    if result_data is None:
        result_data = {
            "fix_type": "unknown",
            "summary": "Follow-up investigation did not produce structured results.",
            "confidence": 0.0,
            "recommended_actions": ["Manual investigation required"],
            "relevant_services": [],
        }

    result_data["cost_usd"] = total_cost
    result_data["duration_ms"] = elapsed_ms
    result_data.setdefault("relevant_services", [])
    result_data.setdefault("recommended_actions", [])
    result_data["claude_session_id"] = new_session_id or claude_session_id

    return result_data
