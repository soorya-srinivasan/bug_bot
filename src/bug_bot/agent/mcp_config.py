from bug_bot.config import settings


def build_mcp_servers() -> dict:
    """Build MCP server configurations for Claude Agent SDK."""
    servers = {}

    # # GitHub (official Anthropic MCP server)
    # if settings.github_token:
    #     servers["github"] = {
    #         "command": "npx",
    #         "args": ["-y", "@anthropic-ai/mcp-server-github"],
    #         "env": {"GITHUB_TOKEN": settings.github_token},
    #     }

    # # Git (official Anthropic MCP server)
    # servers["git"] = {
    #     "command": "npx",
    #     "args": ["-y", "@anthropic-ai/mcp-server-git"],
    # }

    # # Grafana — API key is optional (works with anonymous admin access)
    # if settings.grafana_url:
    #     grafana_env = {"GRAFANA_URL": settings.grafana_url}
    #     if settings.grafana_api_key:
    #         grafana_env["GRAFANA_API_KEY"] = settings.grafana_api_key
    #     servers["grafana"] = {
    #         "command": "npx",
    #         "args": ["-y", "@grafana/mcp-grafana"],
    #         "env": grafana_env,
    #     }

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
