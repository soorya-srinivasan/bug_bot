from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Slack
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    slack_app_token: str = ""
    slack_socket_mode: bool = False
    bug_reports_channel_id: str = ""
    bug_summaries_channel_id: str = ""
    # Slack bot user ID (e.g. "U08XXXXX"). Required when require_bot_mention=True.
    slack_bot_user_id: str = ""
    # Feature flag: when True, only messages that @mention the bot in #bug-reports trigger an investigation.
    # When False (default), all top-level messages in #bug-reports are treated as bug reports.
    require_bot_mention: bool = False
    # How the investigation summary is posted to #bug-summaries.
    # "flat"     – header + detail blocks in a single message (default, current behaviour).
    # "threaded" – brief header message, then full detail blocks as a thread reply.
    # "canvas"   – brief header message, then a Markdown file uploaded into the thread.
    summary_post_mode: str = "threaded"

    # Database
    database_url: str = "postgresql+asyncpg://bugbot:bugbot@localhost:5432/bugbot"

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "bug-investigation"

    # Anthropic
    anthropic_api_key: str = ""
    claude_cli_path: str = "/usr/local/bin/claude"

    # GitHub
    github_token: str = ""
    github_org: str = ""

    # Grafana
    grafana_url: str = ""
    grafana_api_key: str = ""

    # New Relic
    newrelic_api_key: str = ""
    newrelic_account_id: str = ""

    # Database MCP (read-only)
    postgres_readonly_url: str = ""
    mysql_readonly_url: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    anthropic_log: str = "info"


settings = Settings()
