from bug_bot.config import settings


def build_mcp_servers() -> dict:
    """Build MCP server configurations for Claude Agent SDK."""
    servers = {}

    # GitHub MCP server — handles ALL repository operations via the GitHub API:
    # branch creation, file reads, file commits (create_or_update_file / push_files),
    # and PR creation.  No local git clone or Bash git commands are needed.
    # Package: @modelcontextprotocol/server-github (npm, confirmed valid)
    if settings.github_token:
        servers["github"] = {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            # @modelcontextprotocol/server-github reads GITHUB_PERSONAL_ACCESS_TOKEN,
            # not GITHUB_TOKEN.  Using the wrong var leaves every request unauthenticated:
            # public reads succeed, but any write (create_branch, push_files, create_pull_request)
            # returns HTTP 401 / GitHubAuthenticationError.
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": settings.github_token},
        }

    # NOTE: mcp-server-git (uvx) was removed.
    # @modelcontextprotocol/server-git has no git_clone or git_push tools, making it
    # unusable for the full PR workflow.  All git operations now go through the
    # GitHub MCP server above (API-based, no local filesystem required).

    # Grafana MCP via npx is disabled — @grafana/mcp-grafana does not exist on npm.
    # Loki and Grafana are queried directly via the bugbot_tools custom MCP server
    # (query_loki_logs and list_datasources tools in tools.py).

    # # New Relic
    # if settings.newrelic_api_key:
    #     servers["newrelic"] = {
    #         "command": "npx",
    #         "args": ["-y", "newrelic-mcp-server"],
    #         "env": {
    #             "NEW_RELIC_API_KEY": settings.newrelic_api_key,
    #             "NEW_RELIC_ACCOUNT_ID": settings.newrelic_account_id,
    #         },
    #     }

    # # PostgreSQL (read-only via DBHub)
    # if settings.postgres_readonly_url:
    #     servers["vigeon2"] = {
    #         "command": "npx",
    #         "args": ["-y", "@modelcontextprotocol/server-postgres"],
    #         "env": {"DATABASE_URL": settings.postgres_readonly_url},
    #     }

    # # MySQL (read-only via DBHub)
    # if settings.mysql_readonly_url:
    #     servers["mysql"] = {
    #         "command": "npx",
    #         "args": ["-y", "@modelcontextprotocol/server-postgres"],
    #         "env": {"DATABASE_URL": settings.mysql_readonly_url},
    #     }

    return servers
