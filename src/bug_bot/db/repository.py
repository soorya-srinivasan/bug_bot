from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.models.models import BugReport, BugConversation, Investigation, SLAConfig, Escalation, ServiceTeamMapping


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
        status: str = "new",
        workflow_id: str | None = None,
        attachments: list[dict] | None = None,
    ) -> BugReport:
        report = BugReport(
            bug_id=bug_id,
            slack_channel_id=channel_id,
            slack_thread_ts=thread_ts,
            reporter_user_id=reporter,
            original_message=message,
            severity=severity,
            status=status,
            temporal_workflow_id=workflow_id,
            attachments=attachments or [],
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
            conversation_history=result.get("conversation_history"),
            summary_thread_ts=result.get("summary_thread_ts"),
            claude_session_id=result.get("claude_session_id"),
        )
        self.session.add(investigation)
        await self.session.commit()
        return investigation

    async def get_claude_session_id(self, bug_id: str) -> str | None:
        stmt = select(Investigation.claude_session_id).where(Investigation.bug_id == bug_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_sla_config(self, severity: str) -> SLAConfig | None:
        stmt = select(SLAConfig).where(SLAConfig.severity == severity, SLAConfig.is_active == True)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_service_mapping(self, service_name: str) -> ServiceTeamMapping | None:
        stmt = select(ServiceTeamMapping).where(ServiceTeamMapping.service_name == service_name)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_bug_by_thread_ts(self, channel_id: str, thread_ts: str) -> BugReport | None:
        stmt = select(BugReport).where(
            BugReport.slack_channel_id == channel_id,
            BugReport.slack_thread_ts == thread_ts,
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_bug_by_summary_thread_ts(self, summary_thread_ts: str) -> BugReport | None:
        stmt = (
            select(BugReport)
            .join(Investigation, Investigation.bug_id == BugReport.bug_id)
            .where(Investigation.summary_thread_ts == summary_thread_ts)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_investigation(self, bug_id: str) -> Investigation | None:
        stmt = select(Investigation).where(Investigation.bug_id == bug_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_investigation_conversation(
        self, bug_id: str, conversation_history: list[dict]
    ) -> None:
        stmt = (
            update(Investigation)
            .where(Investigation.bug_id == bug_id)
            .values(conversation_history=conversation_history)
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def store_summary_thread_ts(self, bug_id: str, summary_thread_ts: str) -> None:
        stmt = (
            update(Investigation)
            .where(Investigation.bug_id == bug_id)
            .values(summary_thread_ts=summary_thread_ts)
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def get_bug_by_id(self, bug_id: str) -> BugReport | None:
        stmt = select(BugReport).where(BugReport.bug_id == bug_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_conversations(self, bug_id: str) -> list[BugConversation]:
        result = await self.session.execute(
            select(BugConversation)
            .where(BugConversation.bug_id == bug_id)
            .order_by(BugConversation.created_at)
        )
        return list(result.scalars().all())

    async def log_conversation(
        self,
        bug_id: str,
        message_type: str,
        sender_type: str,
        sender_id: str | None = None,
        channel: str | None = None,
        message_text: str | None = None,
        metadata: dict | None = None,
    ) -> BugConversation:
        entry = BugConversation(
            bug_id=bug_id,
            message_type=message_type,
            sender_type=sender_type,
            sender_id=sender_id,
            channel=channel,
            message_text=message_text,
            metadata_=metadata,
        )
        self.session.add(entry)
        await self.session.commit()
        return entry
