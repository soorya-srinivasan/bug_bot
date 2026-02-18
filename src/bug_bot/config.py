from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Slack
    slack_bot_token: str = ""
    slack_signing_secret: str = ""
    bug_reports_channel_id: str = ""
    bug_summaries_channel_id: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://bugbot:bugbot@localhost:5432/bugbot"

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "bug-investigation"

    # Anthropic
    anthropic_api_key: str = ""

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


settings = Settings()
