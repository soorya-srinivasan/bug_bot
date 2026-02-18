# Bug Bot - Detailed Implementation Plan (Local Demo)

## Context

Build an automated bug investigation agent for ShopTech that receives bug reports from Slack, autonomously investigates using AI (Claude Agent SDK), integrates with observability tools (Grafana, New Relic), queries databases and GitHub repos, creates PRs for fixes, escalates to humans when needed, and tracks SLAs with periodic follow-ups.

**Platform context:** .NET microservices (OXO.APIs), Ruby/Rails services (vconnect), single GitHub org, PostgreSQL + MySQL databases, Grafana + New Relic already deployed.

**Scope:** Local development with Docker Compose. No GKE/production deployment in this plan.

---

## Architecture (Local)

```
Slack (#bug-reports)
    │ webhook (via ngrok for local dev)
    ▼
FastAPI + Slack Bolt (localhost:8000)
    │
    ├──→ PostgreSQL (localhost:5432) — app DB
    │
    │ start workflow
    ▼
Temporal Server (localhost:7233, Docker)
    │
    ▼
Temporal Worker (Python process)
    ├── BugInvestigationWorkflow
    │     ├── parse & classify
    │     ├── acknowledge in Slack
    │     ├── Claude Agent SDK ──→ MCP Servers (stdio):
    │     │     ├── GitHub, Git, Grafana, New Relic
    │     │     └── PostgreSQL, MySQL (read-only)
    │     ├── post results to Slack
    │     └── escalate if needed
    └── SLATrackingWorkflow
          └── durable timers, follow-ups, escalation
```

---

## Project Structure

```
bug_bot/
├── pyproject.toml
├── docker-compose.yml              # Temporal + PostgreSQL + app
├── .env                            # Local secrets
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
├── CLAUDE.md                       # Agent project instructions
├── skills/
│   ├── dotnet_debugging.md
│   ├── rails_debugging.md
│   └── database_investigation.md
├── src/bug_bot/
│   ├── __init__.py
│   ├── config.py
│   ├── main.py                     # FastAPI entrypoint
│   ├── worker.py                   # Temporal worker entrypoint
│   ├── slack/
│   │   ├── __init__.py
│   │   ├── app.py
│   │   ├── handlers.py
│   │   └── messages.py
│   ├── temporal/
│   │   ├── __init__.py
│   │   ├── client.py
│   │   ├── workflows/
│   │   │   ├── __init__.py
│   │   │   ├── bug_investigation.py
│   │   │   └── sla_tracking.py
│   │   └── activities/
│   │       ├── __init__.py
│   │       ├── slack_activity.py
│   │       ├── agent_activity.py
│   │       ├── database_activity.py
│   │       └── parsing_activity.py
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── runner.py
│   │   ├── mcp_config.py
│   │   ├── tools.py
│   │   └── prompts.py
│   ├── models/
│   │   ├── __init__.py
│   │   └── models.py
│   ├── db/
│   │   ├── __init__.py
│   │   ├── session.py
│   │   └── repository.py
│   └── sla/
│       ├── __init__.py
│       └── engine.py
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_slack_handlers.py
    ├── test_workflows.py
    └── test_sla_engine.py
```

---

# Phase 1: Foundation

**Goal:** Project scaffold, Slack integration, PostgreSQL setup. Bot acknowledges bug reports.

## 1.1 — pyproject.toml

```toml
# File: pyproject.toml
[project]
name = "bug-bot"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.30.0",
    "slack-bolt>=1.20.0",
    "temporalio>=1.9.0",
    "sqlalchemy[asyncio]>=2.0.0",
    "asyncpg>=0.30.0",
    "alembic>=1.14.0",
    "pydantic>=2.0.0",
    "pydantic-settings>=2.0.0",
    "httpx>=0.28.0",
    "structlog>=24.0.0",
    "claude-agent-sdk>=0.1.36",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24",
    "ruff>=0.8.0",
]

[build-system]
requires = ["setuptools>=75.0"]
build-backend = "setuptools.backends._legacy:_Backend"

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

## 1.2 — .env (local secrets)

```bash
# File: .env

# Slack
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_SIGNING_SECRET=your-signing-secret
BUG_REPORTS_CHANNEL_ID=C0123456789
BUG_SUMMARIES_CHANNEL_ID=C9876543210

# Database (app)
DATABASE_URL=postgresql+asyncpg://bugbot:bugbot@localhost:5432/bugbot

# Temporal
TEMPORAL_HOST=localhost:7233
TEMPORAL_NAMESPACE=default

# Anthropic
ANTHROPIC_API_KEY=sk-ant-your-key

# GitHub
GITHUB_TOKEN=ghp_your-token
GITHUB_ORG=your-org-name

# Grafana
GRAFANA_URL=https://your-grafana.com
GRAFANA_API_KEY=your-grafana-key

# New Relic
NEWRELIC_API_KEY=your-newrelic-key
NEWRELIC_ACCOUNT_ID=your-account-id

# Database connections (read-only, for agent MCP)
POSTGRES_READONLY_URL=postgresql://readonly:pass@localhost:5432/appdb
MYSQL_READONLY_URL=mysql://readonly:pass@localhost:3306/appdb
```

## 1.3 — docker-compose.yml

```yaml
# File: docker-compose.yml
services:
  postgresql:
    image: postgres:16
    environment:
      POSTGRES_USER: bugbot
      POSTGRES_PASSWORD: bugbot
      POSTGRES_DB: bugbot
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

  temporal:
    image: temporalio/auto-setup:latest
    ports:
      - "7233:7233"
    environment:
      - DB=postgres12
      - DB_PORT=5432
      - POSTGRES_USER=bugbot
      - POSTGRES_PWD=bugbot
      - POSTGRES_SEEDS=postgresql
    depends_on:
      - postgresql

  temporal-ui:
    image: temporalio/ui:latest
    ports:
      - "8080:8080"
    environment:
      - TEMPORAL_ADDRESS=temporal:7233
      - TEMPORAL_CORS_ORIGINS=http://localhost:3000
    depends_on:
      - temporal

volumes:
  pgdata:
```

## 1.4 — Config (pydantic-settings)

```python
# File: src/bug_bot/config.py
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Slack
    slack_bot_token: str
    slack_signing_secret: str
    bug_reports_channel_id: str
    bug_summaries_channel_id: str

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
```

## 1.5 — Database Models (SQLAlchemy 2.0)

```python
# File: src/bug_bot/models/models.py
import uuid
from datetime import datetime

from sqlalchemy import String, Text, Float, Integer, Boolean, DateTime, ForeignKey, Index
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class BugReport(Base):
    __tablename__ = "bug_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    slack_channel_id: Mapped[str] = mapped_column(String(20), nullable=False)
    slack_thread_ts: Mapped[str] = mapped_column(String(30), nullable=False)
    reporter_user_id: Mapped[str] = mapped_column(String(20), nullable=False)
    original_message: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(5), nullable=False, default="P3")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    temporal_workflow_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    investigation: Mapped["Investigation | None"] = relationship(back_populates="bug_report")
    escalations: Mapped[list["Escalation"]] = relationship(back_populates="bug_report")

    __table_args__ = (
        Index("idx_bug_reports_status", "status"),
        Index("idx_bug_reports_severity", "severity"),
    )


class Investigation(Base):
    __tablename__ = "investigations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    root_cause: Mapped[str | None] = mapped_column(Text)
    fix_type: Mapped[str] = mapped_column(String(20), nullable=False)
    pr_url: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    relevant_services: Mapped[dict] = mapped_column(JSONB, default=list)
    recommended_actions: Mapped[dict] = mapped_column(JSONB, default=list)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bug_report: Mapped["BugReport"] = relationship(back_populates="investigation")


class SLAConfig(Base):
    __tablename__ = "sla_configs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    severity: Mapped[str] = mapped_column(String(5), unique=True, nullable=False)
    acknowledgement_target_min: Mapped[int] = mapped_column(Integer, nullable=False)
    resolution_target_min: Mapped[int] = mapped_column(Integer, nullable=False)
    follow_up_interval_min: Mapped[int] = mapped_column(Integer, nullable=False)
    escalation_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    escalation_contacts: Mapped[dict] = mapped_column(JSONB, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Escalation(Base):
    __tablename__ = "escalations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bug_id: Mapped[str] = mapped_column(String(50), ForeignKey("bug_reports.bug_id"), nullable=False)
    escalation_level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    escalated_to: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    bug_report: Mapped["BugReport"] = relationship(back_populates="escalations")


class ServiceTeamMapping(Base):
    __tablename__ = "service_team_mapping"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    github_repo: Mapped[str] = mapped_column(String(200), nullable=False)
    team_slack_group: Mapped[str | None] = mapped_column(String(30))
    primary_oncall: Mapped[str | None] = mapped_column(String(20))
    tech_stack: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
```

## 1.6 — Database Session + Repository

```python
# File: src/bug_bot/db/session.py
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bug_bot.config import settings

engine = create_async_engine(settings.database_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
```

```python
# File: src/bug_bot/db/repository.py
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.models.models import BugReport, Investigation, SLAConfig, Escalation, ServiceTeamMapping


class BugRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_bug_report(
        self,
        bug_id: str,
        channel_id: str,
        thread_ts: str,
        reporter: str,
        message: str,
        severity: str = "P3",
        workflow_id: str | None = None,
    ) -> BugReport:
        report = BugReport(
            bug_id=bug_id,
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
            reporter_user_id=reporter,
            original_message=message,
            severity=severity,
            status="new",
            temporal_workflow_id=workflow_id,
        )
        self.session.add(report)
        await self.session.commit()
        await self.session.refresh(report)
        return report

    async def update_status(self, bug_id: str, status: str) -> None:
        stmt = (
            update(BugReport)
            .where(BugReport.bug_id == bug_id)
            .values(status=status, updated_at=datetime.utcnow())
        )
        if status == "resolved":
            stmt = stmt.values(resolved_at=datetime.utcnow())
        await self.session.execute(stmt)
        await self.session.commit()

    async def save_investigation(self, bug_id: str, result: dict) -> Investigation:
        investigation = Investigation(
            bug_id=bug_id,
            root_cause=result.get("root_cause"),
            fix_type=result["fix_type"],
            pr_url=result.get("pr_url"),
            summary=result["summary"],
            confidence=result.get("confidence", 0.0),
            relevant_services=result.get("relevant_services", []),
            recommended_actions=result.get("recommended_actions", []),
            cost_usd=result.get("cost_usd"),
            duration_ms=result.get("duration_ms"),
        )
        self.session.add(investigation)
        await self.session.commit()
        return investigation

    async def get_sla_config(self, severity: str) -> SLAConfig | None:
        stmt = select(SLAConfig).where(SLAConfig.severity == severity, SLAConfig.is_active == True)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_service_mapping(self, service_name: str) -> ServiceTeamMapping | None:
        stmt = select(ServiceTeamMapping).where(ServiceTeamMapping.service_name == service_name)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
```

## 1.7 — Alembic Setup

```python
# File: alembic/env.py
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from bug_bot.config import settings
from bug_bot.models.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=settings.database_url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection):
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(settings.database_url)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

```ini
# File: alembic.ini (relevant section)
[alembic]
script_location = alembic
sqlalchemy.url = postgresql+asyncpg://bugbot:bugbot@localhost:5432/bugbot
```

After setup, generate the initial migration:
```bash
alembic revision --autogenerate -m "initial schema"
alembic upgrade head
```

Then seed SLA defaults:
```sql
INSERT INTO sla_configs (severity, acknowledgement_target_min, resolution_target_min, follow_up_interval_min, escalation_threshold, escalation_contacts) VALUES
('P0', 5,   60,   15, 2, '[{"level": 1, "contacts": ["UENG_LEAD"]}, {"level": 2, "contacts": ["UCTO"]}]'),
('P1', 15,  240,  30, 3, '[{"level": 1, "contacts": ["UTEAM_LEAD"]}, {"level": 2, "contacts": ["UENG_LEAD"]}]'),
('P2', 60,  1440, 120, 3, '[{"level": 1, "contacts": ["UDEV_TEAM"]}]'),
('P3', 240, 4320, 480, 3, '[{"level": 1, "contacts": ["UDEV_TEAM"]}]');
```

## 1.8 — Slack Bolt + FastAPI Integration

```python
# File: src/bug_bot/slack/app.py
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

from bug_bot.config import settings

slack_app = AsyncApp(
    token=settings.slack_bot_token,
    signing_secret=settings.slack_signing_secret,
)

slack_handler = AsyncSlackRequestHandler(slack_app)
```

```python
# File: src/bug_bot/slack/handlers.py
import time

from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from bug_bot.config import settings
from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository


def register_handlers(app: AsyncApp):
    """Register all Slack event handlers on the Bolt app."""

    @app.event("message")
    async def handle_message(event: dict, client: AsyncWebClient):
        # Only process messages from the bug reports channel
        if event.get("channel") != settings.bug_reports_channel_id:
            return

        # Ignore bot messages, thread replies, message edits
        if event.get("bot_id") or event.get("thread_ts") or event.get("subtype"):
            return

        channel_id = event["channel"]
        thread_ts = event["ts"]
        reporter = event.get("user", "unknown")
        text = event.get("text", "")

        # Generate a human-readable bug ID
        bug_id = f"BUG-{int(float(thread_ts))}"

        # Step 1: Acknowledge immediately in thread
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=(
                f":mag: *Bug Bot* received this report (`{bug_id}`).\n"
                f"I'm starting an investigation and will update this thread with findings.\n"
                f"If I need human help, I'll tag the relevant team.\n"
                f"_Report filed by <@{reporter}>_"
            ),
        )

        # Step 2: Save to database
        async with async_session() as session:
            repo = BugRepository(session)
            await repo.create_bug_report(
                bug_id=bug_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                reporter=reporter,
                message=text,
            )

        # Step 3: Start Temporal workflow (added in Phase 2)
        # Will be: await start_investigation_workflow(bug_id, channel_id, thread_ts, text, reporter)
```

```python
# File: src/bug_bot/slack/messages.py
def format_investigation_result(result: dict, bug_id: str) -> list[dict]:
    """Format investigation results as Slack Block Kit blocks."""
    confidence = result.get("confidence", 0)
    if confidence > 0.8:
        confidence_emoji = ":large_green_circle:"
    elif confidence > 0.5:
        confidence_emoji = ":large_yellow_circle:"
    else:
        confidence_emoji = ":red_circle:"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Investigation Results - {bug_id}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Fix Type:* `{result['fix_type']}`"},
                {"type": "mrkdwn", "text": f"*Confidence:* {confidence_emoji} {confidence:.0%}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Summary:*\n{result['summary']}"},
        },
    ]

    if result.get("root_cause"):
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Root Cause:*\n{result['root_cause']}"},
            }
        )

    if result.get("pr_url"):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f":pr: *Pull Request:* <{result['pr_url']}|View PR>",
                },
            }
        )

    if result.get("recommended_actions"):
        actions_text = "\n".join(f"  - {a}" for a in result["recommended_actions"])
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Recommended Actions:*\n{actions_text}"},
            }
        )

    return blocks


def format_summary_message(
    bug_id: str,
    severity: str,
    result: dict,
    original_channel: str,
    original_thread_ts: str,
) -> list[dict]:
    """Format summary message for #bug-summaries channel."""
    status = "Resolved" if result.get("pr_url") else "Escalated"
    thread_link = (
        f"https://slack.com/archives/{original_channel}/p{original_thread_ts.replace('.', '')}"
    )

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{bug_id}* | Severity: `{severity}` | Status: *{status}*\n"
                    f"{result['summary']}\n"
                    f"<{thread_link}|View original thread>"
                ),
            },
        },
    ]

    if result.get("pr_url"):
        blocks[0]["text"]["text"] += f"\n<{result['pr_url']}|View PR>"

    return blocks
```

## 1.9 — FastAPI App

```python
# File: src/bug_bot/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from bug_bot.slack.app import slack_app, slack_handler
from bug_bot.slack.handlers import register_handlers


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    register_handlers(slack_app)
    yield
    # Shutdown


app = FastAPI(title="Bug Bot", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/slack/events")
async def slack_events(request: Request):
    return await slack_handler.handle(request)
```

## 1.10 — Run locally

```bash
# Terminal 1: Start infra
docker compose up -d

# Terminal 2: Run migrations
pip install -e ".[dev]"
alembic upgrade head

# Terminal 3: Start FastAPI
uvicorn bug_bot.main:app --reload --port 8000

# Terminal 4: Expose to Slack via ngrok
ngrok http 8000
# Then set Slack Event Subscription URL to: https://<ngrok-id>.ngrok.io/slack/events
```

---

# Phase 2: Temporal Integration

**Goal:** Durable workflow orchestration. Slack → Temporal → investigation → Slack reply.

## 2.1 — Temporal Client Factory

```python
# File: src/bug_bot/temporal/client.py
from temporalio.client import Client

from bug_bot.config import settings

_client: Client | None = None


async def get_temporal_client() -> Client:
    global _client
    if _client is None:
        _client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
        )
    return _client
```

## 2.2 — Shared Data Types

```python
# File: src/bug_bot/temporal/__init__.py
from dataclasses import dataclass, field


@dataclass
class BugReportInput:
    bug_id: str
    channel_id: str
    thread_ts: str
    message_text: str
    reporter_user_id: str


@dataclass
class ParsedBug:
    bug_id: str
    severity: str
    relevant_services: list[str]
    keywords: list[str]


@dataclass
class InvestigationResult:
    bug_id: str
    root_cause: str | None = None
    fix_type: str = "unknown"  # code_fix, data_fix, config_fix, needs_human, unknown
    pr_url: str | None = None
    summary: str = ""
    confidence: float = 0.0
    recommended_actions: list[str] = field(default_factory=list)
    relevant_services: list[str] = field(default_factory=list)
    cost_usd: float | None = None
    duration_ms: int | None = None


@dataclass
class SLATrackingInput:
    bug_id: str
    severity: str
    channel_id: str
    thread_ts: str
    assigned_users: list[str] = field(default_factory=list)
```

## 2.3 — Activities

```python
# File: src/bug_bot/temporal/activities/parsing_activity.py
import re

from temporalio import activity

from bug_bot.temporal import BugReportInput, ParsedBug

# Keyword → service mapping (extend with your actual services)
SERVICE_KEYWORDS = {
    "payment": "Payment.API",
    "bill": "Bill.API",
    "invoice": "Bill.API",
    "inventory": "Inventory.API",
    "stock": "Inventory.API",
    "auth": "Auth.Server",
    "login": "Auth.Server",
    "subscription": "Subscription.API",
    "company": "Company.API",
    "vconnect": "vconnect",
    "aft": "vconnect-aft",
    "audit": "vconnect-audit",
}

SEVERITY_PATTERNS = {
    "P0": [r"\bcritical\b", r"\bdown\b", r"\boutage\b", r"\bblocking\b", r"\ball users\b"],
    "P1": [r"\burgent\b", r"\bsevere\b", r"\bmajor\b", r"\bproduction\b"],
    "P2": [r"\bbug\b", r"\bissue\b", r"\berror\b", r"\bfailing\b"],
}


@activity.defn
async def parse_bug_report(input: BugReportInput) -> ParsedBug:
    """Parse a raw bug report to extract severity, services, and keywords."""
    text_lower = input.message_text.lower()

    # Detect severity
    severity = "P3"  # default
    for sev, patterns in SEVERITY_PATTERNS.items():
        if any(re.search(p, text_lower) for p in patterns):
            severity = sev
            break

    # Detect relevant services
    services = []
    for keyword, service in SERVICE_KEYWORDS.items():
        if keyword in text_lower and service not in services:
            services.append(service)

    # Extract keywords (simple approach)
    keywords = re.findall(r"\b(?:error|exception|timeout|500|404|null|crash|slow|fail)\b", text_lower)

    activity.logger.info(f"Parsed bug {input.bug_id}: severity={severity}, services={services}")

    return ParsedBug(
        bug_id=input.bug_id,
        severity=severity,
        relevant_services=services,
        keywords=list(set(keywords)),
    )
```

```python
# File: src/bug_bot/temporal/activities/slack_activity.py
from dataclasses import dataclass

from slack_sdk.web.async_client import AsyncWebClient
from temporalio import activity

from bug_bot.config import settings
from bug_bot.slack.messages import format_investigation_result, format_summary_message
from bug_bot.temporal import InvestigationResult


def _get_slack_client() -> AsyncWebClient:
    return AsyncWebClient(token=settings.slack_bot_token)


@dataclass
class PostMessageInput:
    channel_id: str
    thread_ts: str
    text: str


@dataclass
class PostResultsInput:
    channel_id: str
    thread_ts: str
    bug_id: str
    severity: str
    result: dict  # serialized InvestigationResult


@dataclass
class EscalationInput:
    channel_id: str
    thread_ts: str
    bug_id: str
    severity: str
    relevant_services: list[str]
    escalation_level: int = 1


@activity.defn
async def post_slack_message(input: PostMessageInput) -> None:
    """Post a simple text message in a Slack thread."""
    client = _get_slack_client()
    await client.chat_postMessage(
        channel=input.channel_id,
        thread_ts=input.thread_ts,
        text=input.text,
    )


@activity.defn
async def post_investigation_results(input: PostResultsInput) -> None:
    """Post formatted investigation results to Slack thread."""
    client = _get_slack_client()
    blocks = format_investigation_result(input.result, input.bug_id)
    await client.chat_postMessage(
        channel=input.channel_id,
        thread_ts=input.thread_ts,
        blocks=blocks,
        text=f"Investigation complete for {input.bug_id}",  # fallback
    )


@activity.defn
async def create_summary_thread(input: PostResultsInput) -> None:
    """Create a summary post in #bug-summaries channel."""
    client = _get_slack_client()
    blocks = format_summary_message(
        bug_id=input.bug_id,
        severity=input.severity,
        result=input.result,
        original_channel=input.channel_id,
        original_thread_ts=input.thread_ts,
    )
    await client.chat_postMessage(
        channel=settings.bug_summaries_channel_id,
        blocks=blocks,
        text=f"Bug summary: {input.bug_id}",  # fallback
    )


@activity.defn
async def escalate_to_humans(input: EscalationInput) -> None:
    """Tag relevant devs/L1 in the Slack thread."""
    client = _get_slack_client()

    # In production, look up contacts from service_team_mapping + sla_configs
    # For now, post a generic escalation message
    msg = (
        f":rotating_light: *Escalation (Level {input.escalation_level})* for `{input.bug_id}` "
        f"(Severity: `{input.severity}`)\n"
        f"Services: {', '.join(input.relevant_services) or 'Unknown'}\n"
        f"This bug requires human investigation. Please review the analysis above."
    )

    await client.chat_postMessage(
        channel=input.channel_id,
        thread_ts=input.thread_ts,
        text=msg,
    )


@activity.defn
async def send_follow_up(input: PostMessageInput) -> None:
    """Send a periodic follow-up reminder in the thread."""
    client = _get_slack_client()
    await client.chat_postMessage(
        channel=input.channel_id,
        thread_ts=input.thread_ts,
        text=input.text,
    )
```

```python
# File: src/bug_bot/temporal/activities/database_activity.py
from temporalio import activity

from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository


@activity.defn
async def update_bug_status(bug_id: str, status: str) -> None:
    """Update bug report status in the application database."""
    async with async_session() as session:
        repo = BugRepository(session)
        await repo.update_status(bug_id, status)
    activity.logger.info(f"Bug {bug_id} status updated to: {status}")


@activity.defn
async def save_investigation_result(bug_id: str, result: dict) -> None:
    """Save investigation results to the database."""
    async with async_session() as session:
        repo = BugRepository(session)
        await repo.save_investigation(bug_id, result)
    activity.logger.info(f"Investigation saved for bug {bug_id}")


@activity.defn
async def get_sla_config_for_severity(severity: str) -> dict | None:
    """Fetch SLA configuration for a given severity level."""
    async with async_session() as session:
        repo = BugRepository(session)
        config = await repo.get_sla_config(severity)
        if config is None:
            return None
        return {
            "severity": config.severity,
            "acknowledgement_target_min": config.acknowledgement_target_min,
            "resolution_target_min": config.resolution_target_min,
            "follow_up_interval_min": config.follow_up_interval_min,
            "escalation_threshold": config.escalation_threshold,
            "escalation_contacts": config.escalation_contacts,
        }
```

## 2.4 — BugInvestigationWorkflow

```python
# File: src/bug_bot/temporal/workflows/bug_investigation.py
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from bug_bot.temporal import BugReportInput, ParsedBug, InvestigationResult, SLATrackingInput
    from bug_bot.temporal.activities.parsing_activity import parse_bug_report
    from bug_bot.temporal.activities.slack_activity import (
        post_investigation_results,
        create_summary_thread,
        escalate_to_humans,
        PostResultsInput,
        EscalationInput,
    )
    from bug_bot.temporal.activities.database_activity import (
        update_bug_status,
        save_investigation_result,
    )
    from bug_bot.temporal.activities.agent_activity import run_agent_investigation
    from bug_bot.temporal.workflows.sla_tracking import SLATrackingWorkflow


@workflow.defn
class BugInvestigationWorkflow:
    @workflow.run
    async def run(self, input: BugReportInput) -> dict:
        workflow.logger.info(f"Starting investigation for {input.bug_id}")

        # Step 1: Parse and classify the bug report
        parsed: ParsedBug = await workflow.execute_activity(
            parse_bug_report,
            input,
            start_to_close_timeout=timedelta(seconds=30),
        )

        # Step 2: Update DB status to investigating
        await workflow.execute_activity(
            update_bug_status,
            args=[input.bug_id, "investigating"],
            start_to_close_timeout=timedelta(seconds=10),
        )

        # Step 3: Run Claude Agent investigation (long-running)
        investigation_dict: dict = await workflow.execute_activity(
            run_agent_investigation,
            args=[input.bug_id, input.message_text, parsed.severity, parsed.relevant_services],
            start_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=RetryPolicy(
                maximum_attempts=2,
                initial_interval=timedelta(seconds=10),
                backoff_coefficient=2.0,
            ),
        )

        # Step 4: Save investigation to DB
        await workflow.execute_activity(
            save_investigation_result,
            args=[input.bug_id, investigation_dict],
            start_to_close_timeout=timedelta(seconds=10),
        )

        # Step 5: Post results to Slack thread
        results_input = PostResultsInput(
            channel_id=input.channel_id,
            thread_ts=input.thread_ts,
            bug_id=input.bug_id,
            severity=parsed.severity,
            result=investigation_dict,
        )

        await workflow.execute_activity(
            post_investigation_results,
            results_input,
            start_to_close_timeout=timedelta(seconds=15),
        )

        # Step 6: Create summary in #bug-summaries
        await workflow.execute_activity(
            create_summary_thread,
            results_input,
            start_to_close_timeout=timedelta(seconds=15),
        )

        # Step 7: If unresolved, escalate and start SLA tracking
        fix_type = investigation_dict.get("fix_type", "unknown")
        if fix_type in ("needs_human", "unknown"):
            await workflow.execute_activity(
                escalate_to_humans,
                EscalationInput(
                    channel_id=input.channel_id,
                    thread_ts=input.thread_ts,
                    bug_id=input.bug_id,
                    severity=parsed.severity,
                    relevant_services=investigation_dict.get("relevant_services", []),
                ),
                start_to_close_timeout=timedelta(seconds=15),
            )

            # Start child SLA tracking workflow
            await workflow.start_child_workflow(
                SLATrackingWorkflow.run,
                SLATrackingInput(
                    bug_id=input.bug_id,
                    severity=parsed.severity,
                    channel_id=input.channel_id,
                    thread_ts=input.thread_ts,
                    assigned_users=investigation_dict.get("recommended_actions", []),
                ),
                id=f"sla-{input.bug_id}",
            )

            await workflow.execute_activity(
                update_bug_status,
                args=[input.bug_id, "escalated"],
                start_to_close_timeout=timedelta(seconds=10),
            )
        else:
            await workflow.execute_activity(
                update_bug_status,
                args=[input.bug_id, "resolved"],
                start_to_close_timeout=timedelta(seconds=10),
            )

        return investigation_dict
```

## 2.5 — Agent Activity (placeholder for Phase 2, real in Phase 3)

```python
# File: src/bug_bot/temporal/activities/agent_activity.py
from temporalio import activity


@activity.defn
async def run_agent_investigation(
    bug_id: str,
    description: str,
    severity: str,
    relevant_services: list[str],
) -> dict:
    """
    Invoke Claude Agent SDK to investigate the bug.
    Phase 2: Returns placeholder result.
    Phase 3: Will use real Claude Agent SDK with MCP servers.
    """
    activity.logger.info(f"Investigating bug {bug_id} (severity={severity})")

    # ── Phase 2 placeholder ──
    # Replace with real agent invocation in Phase 3
    return {
        "root_cause": "Placeholder — agent investigation not yet implemented",
        "fix_type": "needs_human",
        "pr_url": None,
        "summary": (
            f"Bug `{bug_id}` (severity {severity}) received. "
            f"Potentially affects: {', '.join(relevant_services) or 'unknown services'}. "
            f"Full AI investigation will be available in Phase 3."
        ),
        "confidence": 0.0,
        "recommended_actions": ["Manual investigation required"],
        "relevant_services": relevant_services,
    }
```

## 2.6 — Connect Slack handler to Temporal

Update the Slack handler to start workflows:

```python
# File: src/bug_bot/slack/handlers.py  (UPDATED — replaces Phase 1 version)
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from bug_bot.config import settings
from bug_bot.db.session import async_session
from bug_bot.db.repository import BugRepository
from bug_bot.temporal.client import get_temporal_client
from bug_bot.temporal import BugReportInput
from bug_bot.temporal.workflows.bug_investigation import BugInvestigationWorkflow


def register_handlers(app: AsyncApp):

    @app.event("message")
    async def handle_message(event: dict, client: AsyncWebClient):
        if event.get("channel") != settings.bug_reports_channel_id:
            return
        if event.get("bot_id") or event.get("thread_ts") or event.get("subtype"):
            return

        channel_id = event["channel"]
        thread_ts = event["ts"]
        reporter = event.get("user", "unknown")
        text = event.get("text", "")
        bug_id = f"BUG-{int(float(thread_ts))}"
        workflow_id = f"bug-{thread_ts.replace('.', '-')}"

        # Acknowledge immediately
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=(
                f":mag: *Bug Bot* received this report (`{bug_id}`).\n"
                f"I'm starting an investigation and will update this thread.\n"
                f"If I need human help, I'll tag the relevant team.\n"
                f"_Report filed by <@{reporter}>_"
            ),
        )

        # Save to DB
        async with async_session() as session:
            repo = BugRepository(session)
            await repo.create_bug_report(
                bug_id=bug_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                reporter=reporter,
                message=text,
                workflow_id=workflow_id,
            )

        # Start Temporal workflow
        temporal = await get_temporal_client()
        await temporal.start_workflow(
            BugInvestigationWorkflow.run,
            BugReportInput(
                bug_id=bug_id,
                channel_id=channel_id,
                thread_ts=thread_ts,
                message_text=text,
                reporter_user_id=reporter,
            ),
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
        )

    # Handle !resolve / !close commands in threads
    @app.message(r"!resolve|!close|!fixed")
    async def handle_resolution(event: dict, client: AsyncWebClient):
        if not event.get("thread_ts"):
            return

        thread_ts = event["thread_ts"]
        bug_id = f"BUG-{int(float(thread_ts))}"

        temporal = await get_temporal_client()
        try:
            handle = temporal.get_workflow_handle(f"sla-{bug_id}")
            await handle.signal("mark_resolved")
        except Exception:
            pass  # SLA workflow may not exist

        # Update DB
        async with async_session() as session:
            repo = BugRepository(session)
            await repo.update_status(bug_id, "resolved")

        await client.chat_postMessage(
            channel=event["channel"],
            thread_ts=thread_ts,
            text=":white_check_mark: Bug marked as resolved. SLA tracking stopped.",
        )
```

## 2.7 — Temporal Worker

```python
# File: src/bug_bot/worker.py
import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from bug_bot.config import settings
from bug_bot.temporal.workflows.bug_investigation import BugInvestigationWorkflow
from bug_bot.temporal.workflows.sla_tracking import SLATrackingWorkflow
from bug_bot.temporal.activities.parsing_activity import parse_bug_report
from bug_bot.temporal.activities.slack_activity import (
    post_slack_message,
    post_investigation_results,
    create_summary_thread,
    escalate_to_humans,
    send_follow_up,
)
from bug_bot.temporal.activities.database_activity import (
    update_bug_status,
    save_investigation_result,
    get_sla_config_for_severity,
)
from bug_bot.temporal.activities.agent_activity import run_agent_investigation


async def main():
    logging.basicConfig(level=logging.INFO)

    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[
            BugInvestigationWorkflow,
            SLATrackingWorkflow,
        ],
        activities=[
            parse_bug_report,
            post_slack_message,
            post_investigation_results,
            create_summary_thread,
            escalate_to_humans,
            send_follow_up,
            update_bug_status,
            save_investigation_result,
            get_sla_config_for_severity,
            run_agent_investigation,
        ],
    )

    logging.info(f"Worker started on task queue: {settings.temporal_task_queue}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
```

## 2.8 — Run Phase 2 locally

```bash
# Terminal 1: Docker (Temporal + PG)
docker compose up -d

# Terminal 2: FastAPI
uvicorn bug_bot.main:app --reload --port 8000

# Terminal 3: Temporal Worker
python -m bug_bot.worker

# Terminal 4: ngrok
ngrok http 8000

# Temporal UI at http://localhost:8080 to monitor workflows
```

---

# Phase 3: Claude Agent SDK Integration

**Goal:** Replace the placeholder agent activity with real Claude Agent SDK invocation + MCP servers.

## 3.1 — MCP Server Configuration

```python
# File: src/bug_bot/agent/mcp_config.py
from bug_bot.config import settings


def build_mcp_servers() -> dict:
    """Build MCP server configurations for Claude Agent SDK."""
    servers = {}

    # GitHub (official Anthropic MCP server)
    if settings.github_token:
        servers["github"] = {
            "command": "npx",
            "args": ["-y", "@anthropic-ai/mcp-server-github"],
            "env": {"GITHUB_TOKEN": settings.github_token},
        }

    # Git (official Anthropic MCP server)
    servers["git"] = {
        "command": "npx",
        "args": ["-y", "@anthropic-ai/mcp-server-git"],
    }

    # Grafana
    if settings.grafana_url and settings.grafana_api_key:
        servers["grafana"] = {
            "command": "npx",
            "args": ["-y", "@grafana/mcp-grafana"],
            "env": {
                "GRAFANA_URL": settings.grafana_url,
                "GRAFANA_API_KEY": settings.grafana_api_key,
            },
        }

    # New Relic
    if settings.newrelic_api_key:
        servers["newrelic"] = {
            "command": "npx",
            "args": ["-y", "newrelic-mcp-server"],
            "env": {
                "NEW_RELIC_API_KEY": settings.newrelic_api_key,
                "NEW_RELIC_ACCOUNT_ID": settings.newrelic_account_id,
            },
        }

    # PostgreSQL (read-only via DBHub)
    if settings.postgres_readonly_url:
        servers["postgres"] = {
            "command": "npx",
            "args": ["-y", "@dbhub/dbhub"],
            "env": {"DATABASE_URL": settings.postgres_readonly_url},
        }

    # MySQL (read-only via DBHub)
    if settings.mysql_readonly_url:
        servers["mysql"] = {
            "command": "npx",
            "args": ["-y", "@dbhub/dbhub"],
            "env": {"DATABASE_URL": settings.mysql_readonly_url},
        }

    return servers
```

## 3.2 — Custom Tools

```python
# File: src/bug_bot/agent/tools.py
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
```

## 3.3 — Investigation Prompts

```python
# File: src/bug_bot/agent/prompts.py

def build_investigation_prompt(
    bug_id: str,
    description: str,
    severity: str,
    relevant_services: list[str],
) -> str:
    services_str = ", ".join(relevant_services) if relevant_services else "unknown"

    return f"""Investigate the following bug report and provide a structured analysis.

## Bug Report
- **Bug ID:** {bug_id}
- **Severity:** {severity}
- **Potentially Affected Services:** {services_str}

## Report Content
{description}

## Investigation Steps
Follow these steps in order:

1. **Observability** — Query Grafana dashboards for recent anomalies (error spikes, latency increases, deployment markers) in the affected services. Query New Relic for recent errors, slow transactions, and exception traces.

2. **Code Search** — Search the GitHub organization for repositories related to the affected services. Look for recent commits, open issues, or PRs that might be related.

3. **Code Analysis** — Clone the most relevant repository. Examine the code paths mentioned or implied by the bug report. Look for obvious issues: null references, missing error handling, race conditions, N+1 queries.

4. **Data Check** — If the bug appears data-related, query PostgreSQL and/or MySQL databases (READ-ONLY) to check for data inconsistencies or unexpected values.

5. **Root Cause** — Synthesize your findings into a root cause assessment with a confidence level (0.0-1.0).

6. **Fix** — If the fix is straightforward and you have high confidence (>0.8), create a branch and submit a PR. If the fix is complex or you have low confidence, recommend escalation.

## Important
- All database access is READ-ONLY. Do not attempt writes.
- Use the `lookup_service_owner` tool to find repo and team info for services.
- Use the `report_finding` tool to log significant observations during investigation.
- Be thorough but time-efficient. Focus on the most likely causes first."""
```

## 3.4 — Agent Runner (real Claude Agent SDK)

```python
# File: src/bug_bot/agent/runner.py
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

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            result_data = message.structured_output
            total_cost = getattr(message, "total_cost_usd", None)
            break

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

    return result_data
```

## 3.5 — Update Agent Activity to use real runner

```python
# File: src/bug_bot/temporal/activities/agent_activity.py  (UPDATED — replaces Phase 2 version)
from temporalio import activity

from bug_bot.agent.runner import run_investigation


@activity.defn
async def run_agent_investigation(
    bug_id: str,
    description: str,
    severity: str,
    relevant_services: list[str],
) -> dict:
    """Invoke Claude Agent SDK to investigate the bug."""
    activity.logger.info(
        f"Starting agent investigation for {bug_id} (severity={severity}, "
        f"services={relevant_services})"
    )

    result = await run_investigation(
        bug_id=bug_id,
        description=description,
        severity=severity,
        relevant_services=relevant_services,
    )

    activity.logger.info(
        f"Investigation complete for {bug_id}: fix_type={result['fix_type']}, "
        f"confidence={result.get('confidence', 0)}"
    )

    return result
```

## 3.6 — Skills Files

```markdown
# File: skills/dotnet_debugging.md
# .NET / OXO.APIs Debugging

When investigating bugs in .NET microservices (OXO.APIs):

## Observability
- Check Grafana dashboards for the specific service: request rate, error rate, latency
- Look for recent deployment markers that correlate with the bug report time

## Common Issues
- NullReferenceException in async code paths
- Entity Framework Core N+1 queries (check SQL query logs in Grafana)
- Middleware pipeline ordering issues (check Startup.cs / Program.cs)
- Dependency injection lifetime mismatches (Scoped vs Singleton)
- Connection pool exhaustion (check active connection count metrics)
- Deadlocks from .Result or .Wait() on async code
- Missing null checks on nullable reference types

## Code Structure
- Controllers: /Controllers/
- Services: /Services/
- Data access: /Repositories/ or /Data/
- Config: appsettings.json, Startup.cs or Program.cs

## Fix Guidelines
- Follow existing code style (check .editorconfig if present)
- Add or update unit tests for the fix
- Include bug ID in PR description and branch name
```

```markdown
# File: skills/rails_debugging.md
# Ruby on Rails / vconnect Debugging

When investigating bugs in Rails services (vconnect):

## Observability
- Check New Relic APM: error rate, transaction traces, slow queries
- Look at recent deployments in New Relic deployment markers

## Common Issues
- N+1 queries (fix with includes/eager_load)
- Missing database indexes (check db/schema.rb)
- Background job failures (Sidekiq/Resque — check dead letter queue)
- Memory bloat from large ActiveRecord result sets (use find_each/in_batches)
- Race conditions in concurrent request handling
- Missing model validations leading to bad data
- Gem version incompatibilities after bundle update

## Code Structure
- Models: app/models/
- Controllers: app/controllers/
- Services: app/services/
- Jobs: app/jobs/ or app/workers/
- Migrations: db/migrate/
- Config: config/

## Fix Guidelines
- Follow existing Ruby style (check .rubocop.yml)
- Add or update RSpec tests
- Include bug ID in PR description and branch name
```

```markdown
# File: skills/database_investigation.md
# Database Investigation

When investigating data-related bugs:

## PostgreSQL
- Check for recent schema changes or migrations
- Look for constraint violations in error logs
- Check for long-running queries or locks (pg_stat_activity)
- Verify indexes exist for commonly queried columns
- Check for data inconsistencies between related tables

## MySQL
- Check for deadlocks in SHOW ENGINE INNODB STATUS
- Look for slow queries in the slow query log
- Verify foreign key constraints are enforced
- Check character encoding issues (utf8 vs utf8mb4)

## General
- All queries MUST be read-only (SELECT only)
- Always LIMIT result sets to avoid pulling excessive data
- Check for NULL values in columns that should have defaults
- Look for orphaned records (foreign key references to deleted rows)
- Compare timestamps to find data that was modified at bug report time
```

## 3.7 — CLAUDE.md (project instructions for the agent)

```markdown
# File: CLAUDE.md
# Bug Bot - Project Instructions

You are Bug Bot, an automated bug investigation agent for ShopTech.

## Platform Overview
- **OXO.APIs**: .NET 8.0 microservices (Payment, Bill, Inventory, Auth, Subscription, Company)
- **vconnect**: Ruby on Rails services (AFT, audit, various business modules)
- **GitHub**: All repos under a single organization

## Available MCP Servers
- **grafana**: Query dashboards, panels, and Loki logs
- **newrelic**: NRQL queries, APM data, error tracking
- **github**: Search repos, issues, PRs; create issues and PRs
- **git**: Clone repos, create branches, commit, push
- **postgres**: Read-only PostgreSQL queries
- **mysql**: Read-only MySQL queries

## Investigation Protocol
1. Always start with observability data (Grafana + New Relic)
2. Search GitHub for relevant code before cloning entire repos
3. Use lookup_service_owner to find team info and repo for a service
4. All database access is READ-ONLY
5. If creating a fix, create a PR with a clear description referencing the bug ID
6. If unsure, recommend escalation rather than making incorrect changes

## Skills
Check the /skills directory for platform-specific debugging guides.
```

## 3.8 — Prerequisites for Phase 3

Before running, ensure these are installed locally:
```bash
# Claude Code CLI (required by claude-agent-sdk)
npm install -g @anthropic-ai/claude-code

# MCP servers will be auto-installed via npx, but you can pre-install:
npm install -g @anthropic-ai/mcp-server-github
npm install -g @anthropic-ai/mcp-server-git
npm install -g @dbhub/dbhub
```

---

# Phase 4: PR Creation & Code Fix Pipeline

**Goal:** Agent can clone repos, create branches, make code fixes, and submit PRs.

## 4.1 — GitHub PR Skill

The agent already has the GitHub MCP and Git MCP servers. Phase 4 is about adding a skill that guides the agent on the PR creation workflow.

```markdown
# File: skills/pr_creation.md
# Pull Request Creation Guide

When you identify a code fix for a bug:

## Workflow
1. Use the GitHub MCP to find the correct repository
2. Clone the repo using the Git MCP: `git clone <repo_url> /tmp/bugbot-workspace/<repo_name>`
3. Create a branch: `git checkout -b bugbot/<bug_id>-<short-description>`
4. Make the code changes using Write/Edit tools
5. Run any existing tests if a test runner is configured
6. Commit with message: `fix(<service>): <description> [BUG-XXXX]`
7. Push the branch: `git push origin bugbot/<bug_id>-<short-description>`
8. Create a PR via GitHub MCP with this template:

## PR Template
Title: `fix(<service>): <short description> [BUG-XXXX]`

Body:
```
## Bug Report
- Bug ID: <bug_id>
- Severity: <severity>
- Original report: <slack_thread_link>

## Root Cause
<explanation of the root cause>

## Fix
<explanation of what was changed and why>

## Testing
- [ ] Existing tests pass
- [ ] New tests added for the fix (if applicable)

---
*Automated fix by Bug Bot*
```

## Guidelines
- NEVER force push
- NEVER commit to main/master directly
- Keep changes minimal — only fix the reported bug
- If tests fail, include the failure in the PR description and mark as draft
- Assign the PR to the service team (use lookup_service_owner)
```

## 4.2 — Enhanced System Prompt for PR Creation

Update the system prompt in `runner.py` to include PR guidance:

```python
# In src/bug_bot/agent/runner.py — update the system_prompt in ClaudeAgentOptions:

system_prompt=(
    "You are Bug Bot, an automated bug investigation agent for ShopTech. "
    "You have access to Grafana, New Relic, GitHub, Git, PostgreSQL, and MySQL "
    "via MCP servers. Your goal is to investigate bug reports, identify root causes, "
    "and create fixes when possible.\n\n"
    "For .NET services (OXO.APIs): check Grafana dashboards, look for common C#/.NET issues.\n"
    "For Ruby/Rails services (vconnect): check New Relic APM, look for Rails-specific issues.\n\n"
    "IMPORTANT: When creating code fixes:\n"
    f"- Clone repos to /tmp/bugbot-workspace/\n"
    f"- Branch naming: bugbot/<bug_id>-<short-desc>\n"
    f"- Commit message: fix(<service>): <desc> [<bug_id>]\n"
    "- Create a PR with the bug ID in the title\n"
    "- Keep changes minimal — only fix the reported issue\n"
    "- Never push to main/master directly\n\n"
    "Always provide your findings in a structured format at the end."
),
```

No additional code changes are needed — the agent uses the Git MCP to clone/branch/commit/push and the GitHub MCP to create PRs. The skills files provide the guidance.

---

# Phase 5: SLA Tracking & Escalation

**Goal:** Automated SLA tracking with durable timers, periodic follow-ups, multi-level escalation, and resolution commands.

## 5.1 — SLA Engine

```python
# File: src/bug_bot/sla/engine.py
from datetime import datetime, timedelta, timezone


def calculate_sla_status(
    created_at: datetime,
    resolution_target_min: int,
    now: datetime | None = None,
) -> dict:
    """Calculate SLA status for a bug report."""
    if now is None:
        now = datetime.now(timezone.utc)

    elapsed = now - created_at
    target = timedelta(minutes=resolution_target_min)
    remaining = target - elapsed

    return {
        "is_breached": elapsed > target,
        "remaining_minutes": max(0, remaining.total_seconds() / 60),
        "elapsed_minutes": elapsed.total_seconds() / 60,
        "percentage_elapsed": min(100, (elapsed / target) * 100),
    }


# Default SLA configs (used if DB config is missing)
DEFAULT_SLA = {
    "P0": {"ack_min": 5, "resolution_min": 60, "follow_up_min": 15, "escalation_threshold": 2},
    "P1": {"ack_min": 15, "resolution_min": 240, "follow_up_min": 30, "escalation_threshold": 3},
    "P2": {"ack_min": 60, "resolution_min": 1440, "follow_up_min": 120, "escalation_threshold": 3},
    "P3": {"ack_min": 240, "resolution_min": 4320, "follow_up_min": 480, "escalation_threshold": 3},
}
```

## 5.2 — SLA Tracking Workflow

```python
# File: src/bug_bot/temporal/workflows/sla_tracking.py
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from bug_bot.temporal import SLATrackingInput
    from bug_bot.temporal.activities.slack_activity import (
        send_follow_up,
        escalate_to_humans,
        PostMessageInput,
        EscalationInput,
    )
    from bug_bot.temporal.activities.database_activity import (
        get_sla_config_for_severity,
        update_bug_status,
    )
    from bug_bot.sla.engine import DEFAULT_SLA


@workflow.defn
class SLATrackingWorkflow:

    def __init__(self):
        self._resolved = False

    @workflow.signal
    async def mark_resolved(self) -> None:
        self._resolved = True

    @workflow.run
    async def run(self, input: SLATrackingInput) -> str:
        workflow.logger.info(f"SLA tracking started for {input.bug_id} (severity={input.severity})")

        # Fetch SLA config from DB
        sla_config = await workflow.execute_activity(
            get_sla_config_for_severity,
            input.severity,
            start_to_close_timeout=timedelta(seconds=10),
        )

        # Fallback to defaults if DB config missing
        if sla_config is None:
            defaults = DEFAULT_SLA.get(input.severity, DEFAULT_SLA["P3"])
            follow_up_interval = defaults["follow_up_min"]
            escalation_threshold = defaults["escalation_threshold"]
            resolution_target = defaults["resolution_min"]
        else:
            follow_up_interval = sla_config["follow_up_interval_min"]
            escalation_threshold = sla_config["escalation_threshold"]
            resolution_target = sla_config["resolution_target_min"]

        follow_up_count = 0
        escalation_level = 0
        total_elapsed_min = 0

        while not self._resolved:
            # Durable sleep — survives worker restarts
            await workflow.sleep(timedelta(minutes=follow_up_interval))
            total_elapsed_min += follow_up_interval

            if self._resolved:
                break

            follow_up_count += 1

            # Check if we need to escalate
            if follow_up_count >= escalation_threshold:
                escalation_level += 1
                follow_up_count = 0  # Reset counter after escalation

                await workflow.execute_activity(
                    escalate_to_humans,
                    EscalationInput(
                        channel_id=input.channel_id,
                        thread_ts=input.thread_ts,
                        bug_id=input.bug_id,
                        severity=input.severity,
                        relevant_services=[],
                        escalation_level=escalation_level,
                    ),
                    start_to_close_timeout=timedelta(seconds=15),
                )

                workflow.logger.info(
                    f"Escalated {input.bug_id} to level {escalation_level}"
                )
            else:
                # Send follow-up reminder
                await workflow.execute_activity(
                    send_follow_up,
                    PostMessageInput(
                        channel_id=input.channel_id,
                        thread_ts=input.thread_ts,
                        text=(
                            f":clock3: *SLA Follow-up #{follow_up_count}* for `{input.bug_id}` "
                            f"(Severity: `{input.severity}`)\n"
                            f"This bug has been open for {total_elapsed_min} minutes. "
                            f"Resolution target: {resolution_target} minutes.\n"
                            f"Reply `!resolve` in this thread when fixed."
                        ),
                    ),
                    start_to_close_timeout=timedelta(seconds=15),
                )

            # Check SLA breach
            if total_elapsed_min >= resolution_target:
                await workflow.execute_activity(
                    send_follow_up,
                    PostMessageInput(
                        channel_id=input.channel_id,
                        thread_ts=input.thread_ts,
                        text=(
                            f":rotating_light: *SLA BREACHED* for `{input.bug_id}` "
                            f"(Severity: `{input.severity}`)\n"
                            f"Resolution target of {resolution_target} minutes has been exceeded. "
                            f"Elapsed: {total_elapsed_min} minutes."
                        ),
                    ),
                    start_to_close_timeout=timedelta(seconds=15),
                )

                await workflow.execute_activity(
                    update_bug_status,
                    args=[input.bug_id, "sla_breached"],
                    start_to_close_timeout=timedelta(seconds=10),
                )

        workflow.logger.info(f"Bug {input.bug_id} resolved. SLA tracking complete.")
        return f"Bug {input.bug_id} resolved after {total_elapsed_min} minutes"
```

## 5.3 — Admin API for SLA Config

```python
# File: src/bug_bot/api/routes.py
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.db.session import get_session
from bug_bot.models.models import SLAConfig, BugReport

router = APIRouter()


class SLAConfigUpdate(BaseModel):
    acknowledgement_target_min: int | None = None
    resolution_target_min: int | None = None
    follow_up_interval_min: int | None = None
    escalation_threshold: int | None = None
    escalation_contacts: list[dict] | None = None


@router.get("/sla-configs")
async def list_sla_configs(session: AsyncSession = Depends(get_session)):
    result = await session.execute(select(SLAConfig).order_by(SLAConfig.severity))
    configs = result.scalars().all()
    return [
        {
            "severity": c.severity,
            "acknowledgement_target_min": c.acknowledgement_target_min,
            "resolution_target_min": c.resolution_target_min,
            "follow_up_interval_min": c.follow_up_interval_min,
            "escalation_threshold": c.escalation_threshold,
            "escalation_contacts": c.escalation_contacts,
            "is_active": c.is_active,
        }
        for c in configs
    ]


@router.patch("/sla-configs/{severity}")
async def update_sla_config(
    severity: str,
    body: SLAConfigUpdate,
    session: AsyncSession = Depends(get_session),
):
    result = await session.execute(select(SLAConfig).where(SLAConfig.severity == severity))
    config = result.scalar_one_or_none()
    if config is None:
        return {"error": f"No SLA config for severity: {severity}"}, 404

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(config, field, value)

    await session.commit()
    return {"status": "updated", "severity": severity}


@router.get("/bugs")
async def list_bugs(
    status: str | None = None,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(BugReport).order_by(BugReport.created_at.desc()).limit(50)
    if status:
        stmt = stmt.where(BugReport.status == status)
    result = await session.execute(stmt)
    bugs = result.scalars().all()
    return [
        {
            "bug_id": b.bug_id,
            "severity": b.severity,
            "status": b.status,
            "reporter": b.reporter_user_id,
            "created_at": b.created_at.isoformat() if b.created_at else None,
            "resolved_at": b.resolved_at.isoformat() if b.resolved_at else None,
        }
        for b in bugs
    ]
```

## 5.4 — Update FastAPI main.py to include admin routes

```python
# File: src/bug_bot/main.py  (UPDATED — replaces Phase 1 version)
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from bug_bot.slack.app import slack_app, slack_handler
from bug_bot.slack.handlers import register_handlers
from bug_bot.api.routes import router as api_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    register_handlers(slack_app)
    yield


app = FastAPI(title="Bug Bot", lifespan=lifespan)

# Admin API
app.include_router(api_router, prefix="/api")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/slack/events")
async def slack_events(request: Request):
    return await slack_handler.handle(request)
```

---

# Running the Full Local Demo

## Prerequisites

```bash
# 1. Python 3.12+
python --version

# 2. Node.js 20+ (for MCP servers)
node --version

# 3. Claude Code CLI
npm install -g @anthropic-ai/claude-code

# 4. Docker
docker --version

# 5. ngrok (for Slack webhook tunneling)
ngrok --version
```

## Setup

```bash
# Clone / init project
cd /Users/suriyasrinivasan/shoptech/bug_bot
pip install -e ".[dev]"

# Start infrastructure
docker compose up -d

# Wait for Temporal to be ready (~30s), then run migrations
alembic upgrade head

# Seed SLA defaults (run via psql or any PG client)
psql postgresql://bugbot:bugbot@localhost:5432/bugbot -c "
INSERT INTO sla_configs (severity, acknowledgement_target_min, resolution_target_min, follow_up_interval_min, escalation_threshold, escalation_contacts) VALUES
('P0', 5,   60,   15, 2, '[{\"level\": 1, \"contacts\": [\"UENG_LEAD\"]}]'),
('P1', 15,  240,  30, 3, '[{\"level\": 1, \"contacts\": [\"UTEAM_LEAD\"]}]'),
('P2', 60,  1440, 120, 3, '[{\"level\": 1, \"contacts\": [\"UDEV_TEAM\"]}]'),
('P3', 240, 4320, 480, 3, '[{\"level\": 1, \"contacts\": [\"UDEV_TEAM\"]}]')
ON CONFLICT (severity) DO NOTHING;
"
```

## Run (3 terminals)

```bash
# Terminal 1: FastAPI
uvicorn bug_bot.main:app --reload --port 8000

# Terminal 2: Temporal Worker
python -m bug_bot.worker

# Terminal 3: ngrok tunnel
ngrok http 8000
# Copy the HTTPS URL → set as Slack Event Subscription URL:
# https://<id>.ngrok.io/slack/events
```

## Verify

1. **Slack**: Post a message in `#bug-reports` → bot acknowledges in thread
2. **Temporal UI**: http://localhost:8080 → see BugInvestigationWorkflow running
3. **Investigation**: Agent runs, posts results in thread
4. **Summary**: Check `#bug-summaries` for cross-posted summary
5. **SLA**: Wait for follow-up interval → see follow-up messages in thread
6. **Resolve**: Reply `!resolve` in thread → SLA tracking stops
7. **Admin API**: `curl http://localhost:8000/api/bugs` → see tracked bugs
8. **Admin API**: `curl http://localhost:8000/api/sla-configs` → see SLA configs

## Slack App Setup Checklist

1. Create a Slack App at https://api.slack.com/apps
2. Enable **Event Subscriptions** → set URL to `https://<ngrok>/slack/events`
3. Subscribe to bot events: `message.channels`, `message.groups`
4. Add **Bot Token Scopes**: `chat:write`, `channels:history`, `groups:history`, `channels:read`
5. Install to workspace, copy Bot Token and Signing Secret to `.env`
6. Invite the bot to `#bug-reports` and `#bug-summaries` channels
