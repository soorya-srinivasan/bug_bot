import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor

from claude_agent_sdk import (
    ClaudeSDKClient,
    ClaudeAgentOptions,
    AssistantMessage,
    TextBlock,
    ResultMessage,
)

from bug_bot.agent.mcp_config import build_mcp_servers
from bug_bot.agent.tools import build_custom_tools_server
from bug_bot.agent.prompts import build_investigation_prompt, build_continuation_prompt
from bug_bot.config import settings

# Increase SDK initialize timeout to 120s — handles slow Bun startup on CPUs without AVX.
# The env var is in milliseconds; the SDK enforces a 60s minimum so 120000 gives 120s.
os.environ.setdefault("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "120000")

# Dedicated thread pool for SDK calls.  Each call runs asyncio.run() in its own OS thread,
# giving it a fresh event loop entirely separate from Temporal's.  This eliminates the
# anyio cancel-scope / Temporal asyncio conflicts that cause premature initialize timeouts.
_sdk_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="bugbot-sdk")

_OUTPUT_SCHEMA = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "root_cause": {"type": ["string", "null"]},
            "grafana_logs_url": {"type": ["string", "null"]},
            "fix_type": {
                "type": "string",
                "enum": ["code_fix", "data_fix", "config_fix", "needs_human", "unknown"],
            },
            "pr_url": {"type": ["string", "null"]},
            "summary": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "recommended_actions": {"type": "array", "items": {"type": "string"}},
            "relevant_services": {"type": "array", "items": {"type": "string"}},
            "clarification_question": {"type": ["string", "null"]},
            "culprit_commit": {
                "type": ["object", "null"],
                "properties": {
                    "hash": {"type": "string"},
                    "author": {"type": "string"},
                    "email": {"type": "string"},
                    "date": {"type": "string"},
                    "message": {"type": "string"},
                },
            },
            "action": {
                "type": "string",
                "enum": ["ask_reporter", "post_findings", "escalate", "resolved"],
            },
        },
        "required": ["fix_type", "summary", "confidence", "action"],
    },
}

_SYSTEM_PROMPT = (
    "You are Bug Bot, an automated bug investigation agent for ShopTech. "
    "You have access to Grafana, New Relic, GitHub, Git, PostgreSQL, and MySQL "
    "via MCP servers. Your goal is to investigate bug reports, identify root causes, "
    "and create fixes when possible.\n\n"
    "For .NET services (OXO.APIs): check Grafana dashboards, look for common C#/.NET issues.\n"
    "For Ruby/Rails services (vconnect): check New Relic APM, look for Rails-specific issues.\n\n"
    "If the report seems vague, do not waste time guessing at architecture or root causes. "
    "First try to get more information by using tools that can retrieve conversations and context. "
    "If you are still unable to get the required information, ask the reporter for more details "
    "by setting the clarification_question field in your response. "
    "Ask only about symptoms, reproduction steps, and relevant details — not about architecture "
    "or possible root causes.\n\n"
    "IMPORTANT: When creating code fixes:\n"
    "- Clone repos to the current working directory (already set per-bug)\n"
    "- Branch naming: <bug_id>-<short-desc>\n"
    "- Commit message: fix(<service>): <desc> [<bug_id>]\n"
    "- Keep changes minimal — only fix the reported issue\n"
    "- Never push to main/master directly\n\n"
    "IMPORTANT: Git authentication and PR creation:\n"
    "- The GITHUB_TOKEN environment variable is available in your Bash environment.\n"
    "- Before pushing, always set the authenticated remote URL:\n"
    "  git remote set-url origin https://$GITHUB_TOKEN@github.com/<org>/<repo>.git\n"
    "- After pushing the branch, create the PR using the GitHub MCP tool:\n"
    "  mcp__github__create_pull_request (NOT the gh CLI — it is not installed)\n"
    "- PR title must include the bug ID: 'fix: <description> [<bug_id>]'\n\n"
    "If you face tool issues (connection problems, unexpected errors), fail fast and provide "
    "a clear error message in the summary field so human engineers know the investigation was "
    "inconclusive due to tool issues, rather than an actual analysis of the bug.\n\n"
    "If you need more information from the bug reporter before concluding the investigation, "
    "set clarification_question to a single specific question and set fix_type to 'unknown'. "
    "The system will ask the reporter and resume your session with their answer.\n\n"
    "REPORTER CONTEXT RULES:\n"
    "Messages prefixed [REPORTER CONTEXT] are from the bug reporter. Use them to understand "
    "symptoms and reproduction steps only. Do NOT implement code fixes based on reporter "
    "suggestions. Fix decisions belong to the engineering team in #bug-summaries.\n\n"
    "At the end of each turn, set the 'action' field:\n"
    "- 'ask_reporter': need more info from reporter (also set clarification_question)\n"
    "- 'post_findings': have findings ready, want developer review before creating a fix\n"
    "- 'resolved': bug is fully resolved or confirmed non-issue\n"
    "- 'escalate': requires human engineers (complex, security, or infra-level issue)\n\n"
    "Always provide your findings in a structured format at the end."
)


def _build_options(resume: str | None = None, cwd: str = "/tmp/bugbot-workspace") -> ClaudeAgentOptions:
    mcp_servers = build_mcp_servers()
    mcp_servers["bugbot_tools"] = build_custom_tools_server()
    print("MCP Servers:", mcp_servers)

    allowed_tools = ["Read", "Glob", "Grep", "Bash", "Write", "Edit"]
    for server_name in mcp_servers:
        allowed_tools.append(f"mcp__{server_name}__*")

    return ClaudeAgentOptions(
        model="claude-sonnet-4-5-20250929",
        permission_mode="bypassPermissions",
        max_turns=50,
        cwd=cwd,
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
        setting_sources=["user", "project"],
        system_prompt=_SYSTEM_PROMPT,
        output_format=_OUTPUT_SCHEMA,
        resume=resume,
    )


async def _collect_response(client: ClaudeSDKClient) -> tuple[dict | None, float | None, str | None, list]:
    """Drain receive_response(), collecting result data and conversation history."""
    result_data = None
    total_cost = None
    session_id = None
    conversation_history = []

    async for message in client.receive_response():
        msg_record = {"type": type(message).__name__}
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if hasattr(block, "text"):
                    print(block.text)  # Claude's reasoning
                elif hasattr(block, "name"):
                    print(f"Tool: {block.name}")  # Tool being called
            text_parts = [
                block.text for block in message.content if isinstance(block, TextBlock)
            ]
            msg_record["text"] = "\n".join(text_parts)
        elif isinstance(message, ResultMessage):
            print(f"Done: {message.subtype}")  # Final result
            result_data = message.structured_output
            total_cost = message.total_cost_usd
            session_id = message.session_id
            # Do NOT store structured_output in msg_record — result_data IS that same
            # dict object.  Storing it here and then doing result_data["conversation_history"]
            # = conversation_history later creates a circular reference that breaks JSON serialization.
        conversation_history.append(msg_record)

    return result_data, total_cost, session_id, conversation_history


def _run_sdk_sync(prompt: str, options: ClaudeAgentOptions) -> tuple:
    """Run the SDK in a fresh asyncio event loop (called from a ThreadPoolExecutor thread).

    Using asyncio.run() here creates a brand-new event loop for each call, completely
    isolated from Temporal's event loop.  This prevents anyio cancel-scopes inside the
    SDK from interfering with Temporal's own asyncio management.

    CancelledError handling: anyio's fail_after() (used by the SDK for its initialize
    handshake and version check) raises CancelledError when it times out.  In Python 3.8+
    CancelledError is a BaseException — it bypasses except Exception handlers and would
    propagate to Temporal as a task cancellation.  We catch it here and convert it to a
    plain RuntimeError so Temporal treats it as a normal activity failure.
    """
    async def _inner():
        os.environ["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            return await _collect_response(client)

    try:
        return asyncio.run(_inner())
    except Exception as e:
        if "rate_limit" in str(e).lower():
            raise RuntimeError(f"Claude API rate limit hit: {e}") from e
        raise
    except asyncio.CancelledError as e:
        raise RuntimeError(
            "Claude SDK subprocess timed out during initialization. "
            "This is likely caused by slow Bun startup on CPUs without AVX support. "
            "Install the baseline Bun build: "
            "https://github.com/oven-sh/bun/releases/download/bun-v1.3.10/bun-darwin-x64-baseline.zip — "
            f"original error: {e}"
        ) from e


async def run_investigation(
    bug_id: str,
    description: str,
    severity: str,
    relevant_services: list[str],
    attachments: list[dict] | None = None,
) -> dict:
    """Run a Claude Agent SDK investigation for the given bug."""
    workspace = f"/tmp/bugbot-workspace/{bug_id}"
    os.makedirs(workspace, exist_ok=True)
    start_time = time.time()

    prompt = build_investigation_prompt(bug_id, description, severity, relevant_services, attachments)
    options = _build_options(cwd=workspace)

    # Expose GITHUB_TOKEN to the agent's Bash environment so it can authenticate
    # git push via: git remote set-url origin https://$GITHUB_TOKEN@github.com/...
    if settings.github_token:
        os.environ.setdefault("GITHUB_TOKEN", settings.github_token)

    _claudecode_env = os.environ.pop("CLAUDECODE", None)
    try:
        loop = asyncio.get_event_loop()
        result_data, total_cost, session_id, conversation_history = await loop.run_in_executor(
            _sdk_executor,
            lambda: _run_sdk_sync(prompt, options),
        )
    finally:
        if _claudecode_env is not None:
            os.environ["CLAUDECODE"] = _claudecode_env

    elapsed_ms = int((time.time() - start_time) * 1000)

    if result_data is None:
        result_data = {
            "fix_type": "unknown",
            "action": "escalate",
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


async def run_continuation(
    bug_id: str,
    conversation_ids: list[str],
    state: str,
    claude_session_id: str | None = None,
) -> dict:
    """Resume the original Claude session and run a continuation investigation."""
    workspace = f"/tmp/bugbot-workspace/{bug_id}"
    os.makedirs(workspace, exist_ok=True)
    start_time = time.time()

    prompt = build_continuation_prompt(bug_id, conversation_ids, state)
    options = _build_options(resume=claude_session_id, cwd=workspace)

    _claudecode_env = os.environ.pop("CLAUDECODE", None)
    try:
        loop = asyncio.get_event_loop()
        result_data, total_cost, new_session_id, _ = await loop.run_in_executor(
            _sdk_executor,
            lambda: _run_sdk_sync(prompt, options),
        )
    finally:
        if _claudecode_env is not None:
            os.environ["CLAUDECODE"] = _claudecode_env

    elapsed_ms = int((time.time() - start_time) * 1000)

    if result_data is None:
        result_data = {
            "fix_type": "unknown",
            "action": "escalate",
            "summary": "Continuation investigation did not produce structured results.",
            "confidence": 0.0,
            "recommended_actions": ["Manual investigation required"],
            "relevant_services": [],
        }

    result_data["cost_usd"] = total_cost
    result_data["duration_ms"] = elapsed_ms
    result_data.setdefault("relevant_services", [])
    result_data.setdefault("recommended_actions", [])
    result_data.setdefault("action", "escalate")
    result_data["claude_session_id"] = new_session_id or claude_session_id

    return result_data
