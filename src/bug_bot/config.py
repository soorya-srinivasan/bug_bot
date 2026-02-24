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

    # Reporter reply rate limiting
    # Max replies a reporter may submit within reporter_reply_rate_window_secs before
    # the message is silently dropped and a "please wait" reply is returned.
    reporter_reply_rate_limit: int = 3
    reporter_reply_rate_window_secs: int = 300  # 5 minutes

    # Duplicate detection
    # When True, new top-level bug reports are checked against recent open bugs.
    enable_duplicate_detection: bool = False
    # How far back to search for potential duplicates.
    duplicate_check_window_hours: int = 2
    # Minimum Claude-assessed similarity (0.0–1.0) to treat a report as duplicate.
    duplicate_similarity_threshold: float = 0.8

    # Database
    database_url: str = "postgresql+asyncpg://bugbot:bugbot@localhost:5432/bugbot"

    # Temporal
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "bug-investigation"
    # Auto-close
    auto_close_inactivity_days: int = 1  # env: AUTO_CLOSE_INACTIVITY_DAYS

    # Anthropic
    anthropic_api_key: str = ""
    claude_cli_path: str = "/usr/local/bin/claude"

    # GitHub
    github_token: str = ""
    github_org: str = ""

    # Grafana / Loki
    grafana_url: str = "http://localhost:3000"
    grafana_api_key: str = ""
    loki_url: str = "http://localhost:3100"

    # New Relic
    newrelic_api_key: str = ""
    newrelic_account_id: str = ""

    # Database MCP (read-only)
    postgres_readonly_url: str = ""
    mysql_readonly_url: str = ""

    # RAG
    rag_embedding_model: str = "BAAI/bge-base-en-v1.5"
    rag_embedding_dim: int = 768
    rag_top_k: int = 5
    rag_retrieval_k: int = 20  # over-fetch for reranking
    rag_rerank_top_k: int = 5  # final results after reranking
    rag_rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    rag_cache_ttl_seconds: int = 300  # 5 minute TTL for query cache
    rag_bm25_weight: float = 0.3
    rag_semantic_weight: float = 0.7

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    anthropic_log: str = "info"

    # Testing: return static mock responses instead of invoking the Claude agent.
    # Set MOCK_AGENT=true to skip real agent calls and speed up workflow testing.
    mock_agent: bool = False


settings = Settings()
