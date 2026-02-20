from datetime import datetime

from sqlalchemy import Select, desc, func, select, update
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

    async def list_bugs(
        self,
        *,
        status: str | None = None,
        severity: str | None = None,
        service: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
        sort: str = "-created_at",
    ) -> tuple[list[tuple[BugReport, Investigation | None]], int]:
        stmt: Select = select(BugReport, Investigation).join(
            Investigation, Investigation.bug_id == BugReport.bug_id, isouter=True
        )

        if status:
            stmt = stmt.where(BugReport.status == status)
        if severity:
            stmt = stmt.where(BugReport.severity == severity)
        if from_date:
            stmt = stmt.where(BugReport.created_at >= from_date)
        if to_date:
            stmt = stmt.where(BugReport.created_at <= to_date)
        if service:
            stmt = stmt.where(
                Investigation.relevant_services.contains([service])  # type: ignore[arg-type]
            )

        total = await self.session.execute(
            stmt.with_only_columns(func.count()).order_by(None)
        )
        total_count = int(total.scalar_one())

        sort_field = sort.lstrip("+-")
        descending = sort.startswith("-")

        if sort_field == "severity":
            order_col = BugReport.severity
        elif sort_field == "status":
            order_col = BugReport.status
        else:
            order_col = BugReport.created_at

        if descending:
            order_col = desc(order_col)

        stmt = stmt.order_by(order_col)
        offset = (page - 1) * page_size
        stmt = stmt.offset(offset).limit(page_size)

        result = await self.session.execute(stmt)
        rows = result.all()
        return rows, total_count

    async def get_bug_by_id(self, bug_id: str) -> BugReport | None:
        stmt = select(BugReport).where(BugReport.bug_id == bug_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update_bug_admin(
        self,
        bug_id: str,
        *,
        severity: str | None = None,
        status: str | None = None,
    ) -> BugReport | None:
        values: dict = {"updated_at": datetime.utcnow()}
        if severity is not None:
            values["severity"] = severity
        if status is not None:
            values["status"] = status
            if status == "resolved":
                values["resolved_at"] = datetime.utcnow()

        if len(values) == 1:  # only updated_at
            return await self.get_bug_by_id(bug_id)

        stmt = (
            update(BugReport)
            .where(BugReport.bug_id == bug_id)
            .values(**values)
            .returning(BugReport)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one_or_none()

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

    async def list_sla_configs(self, *, is_active: bool | None = None) -> list[SLAConfig]:
        stmt = select(SLAConfig)
        if is_active is not None:
            stmt = stmt.where(SLAConfig.is_active == is_active)
        result = await self.session.execute(stmt.order_by(SLAConfig.severity))
        return list(result.scalars().all())

    async def get_sla_config_by_id(self, id_: str) -> SLAConfig | None:
        stmt = select(SLAConfig).where(SLAConfig.id == id_)  # type: ignore[arg-type]
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_sla_config(self, data: dict) -> SLAConfig:
        config = SLAConfig(**data)
        self.session.add(config)
        await self.session.commit()
        await self.session.refresh(config)
        return config

    async def update_sla_config(self, id_: str, data: dict) -> SLAConfig | None:
        if not data:
            return await self.get_sla_config_by_id(id_)
        stmt = (
            update(SLAConfig)
            .where(SLAConfig.id == id_)  # type: ignore[arg-type]
            .values(**data, updated_at=datetime.utcnow())
            .returning(SLAConfig)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one_or_none()

    async def delete_sla_config(self, id_: str) -> None:
        stmt = (
            update(SLAConfig)
            .where(SLAConfig.id == id_)  # type: ignore[arg-type]
            .values(is_active=False, updated_at=datetime.utcnow())
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def get_service_mapping(self, service_name: str) -> ServiceTeamMapping | None:
        stmt = select(ServiceTeamMapping).where(ServiceTeamMapping.service_name == service_name)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_service_mappings(
        self,
        *,
        service_name: str | None = None,
        tech_stack: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ServiceTeamMapping], int]:
        stmt: Select = select(ServiceTeamMapping)
        if service_name:
            stmt = stmt.where(ServiceTeamMapping.service_name.ilike(f"%{service_name}%"))
        if tech_stack:
            stmt = stmt.where(ServiceTeamMapping.tech_stack == tech_stack)

        total = await self.session.execute(
            stmt.with_only_columns(func.count()).order_by(None)
        )
        total_count = int(total.scalar_one())

        offset = (page - 1) * page_size
        stmt = stmt.order_by(ServiceTeamMapping.service_name).offset(offset).limit(page_size)
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total_count

    async def get_service_mapping_by_id(self, id_: str) -> ServiceTeamMapping | None:
        stmt = select(ServiceTeamMapping).where(ServiceTeamMapping.id == id_)  # type: ignore[arg-type]
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_service_mapping(self, data: dict) -> ServiceTeamMapping:
        mapping = ServiceTeamMapping(**data)
        self.session.add(mapping)
        await self.session.commit()
        await self.session.refresh(mapping)
        return mapping

    async def update_service_mapping(self, id_: str, data: dict) -> ServiceTeamMapping | None:
        if not data:
            return await self.get_service_mapping_by_id(id_)
        stmt = (
            update(ServiceTeamMapping)
            .where(ServiceTeamMapping.id == id_)  # type: ignore[arg-type]
            .values(**data)
            .returning(ServiceTeamMapping)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one_or_none()

    async def delete_service_mapping(self, id_: str) -> None:
        # Hard delete is fine here; mappings can be recreated.
        mapping = await self.get_service_mapping_by_id(id_)
        if mapping is None:
            return
        await self.session.delete(mapping)
        await self.session.commit()

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

    async def create_escalation(
        self,
        bug_id: str,
        *,
        escalation_level: int,
        escalated_to: list[str],
        reason: str | None = None,
    ) -> Escalation:
        escalation = Escalation(
            bug_id=bug_id,
            escalation_level=escalation_level,
            escalated_to=escalated_to,
            reason=reason,
        )
        self.session.add(escalation)
        await self.session.commit()
        await self.session.refresh(escalation)
        return escalation

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
