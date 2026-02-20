from datetime import datetime, date

from sqlalchemy import Select, desc, func, select, update, and_, or_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.models.models import (
    BugReport, BugConversation, Investigation, SLAConfig, Escalation,
    ServiceTeamMapping, InvestigationFinding, Team,
    OnCallSchedule, OnCallHistory
)



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

    async def update_assignee(self, bug_id: str, user_id: str) -> None:
        stmt = (
            update(BugReport)
            .where(BugReport.bug_id == bug_id)
            .values(assignee_user_id=user_id, updated_at=datetime.utcnow())
        )
        await self.session.execute(stmt)
        await self.session.commit()

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

    async def get_service_mappings_by_names(self, service_names: list[str]) -> list[ServiceTeamMapping]:
        if not service_names:
            return []
        stmt = (
            select(ServiceTeamMapping)
            .outerjoin(Team, ServiceTeamMapping.team_id == Team.id)
            .options(selectinload(ServiceTeamMapping.team))
            .where(func.lower(ServiceTeamMapping.service_name).in_(
                [s.lower() for s in service_names]
            ))
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

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
        stmt = (
            select(ServiceTeamMapping)
            .options(selectinload(ServiceTeamMapping.team))
            .where(ServiceTeamMapping.id == id_)  # type: ignore[arg-type]
        )
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

    # ── Team CRUD ───────────────────────────────────────────────────────────────

    async def create_team(self, data: dict) -> Team:
        team = Team(**data)
        self.session.add(team)
        await self.session.commit()
        await self.session.refresh(team)
        return team

    async def get_team_by_id(self, id_: str) -> Team | None:
        stmt = select(Team).where(Team.id == id_)  # type: ignore[arg-type]
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_teams(
        self, *, page: int = 1, page_size: int = 50
    ) -> tuple[list[Team], int]:
        stmt: Select = select(Team)
        total = await self.session.execute(
            stmt.with_only_columns(func.count()).order_by(None)
        )
        total_count = int(total.scalar_one())
        offset = (page - 1) * page_size
        stmt = stmt.order_by(Team.created_at).offset(offset).limit(page_size)
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total_count

    async def update_team(self, id_: str, data: dict) -> Team | None:
        if not data:
            return await self.get_team_by_id(id_)
        stmt = (
            update(Team)
            .where(Team.id == id_)  # type: ignore[arg-type]
            .values(**data, updated_at=datetime.utcnow())
            .returning(Team)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one_or_none()

    async def delete_team(self, id_: str) -> None:
        team = await self.get_team_by_id(id_)
        if team is None:
            return
        await self.session.delete(team)
        await self.session.commit()

    async def get_oncall_for_services(self, service_names: list[str]) -> list[dict]:
        """Return deduped on-call info for given services.

        Checks active schedule first, then team oncall_engineer, then service primary_oncall.
        """
        if not service_names:
            return []
        stmt = (
            select(ServiceTeamMapping, Team)
            .outerjoin(Team, ServiceTeamMapping.team_id == Team.id)
            .where(func.lower(ServiceTeamMapping.service_name).in_(
                [s.lower() for s in service_names]
            ))
        )
        results = await self.session.execute(stmt)
        seen: set[str] = set()
        entries: list[dict] = []
        today = date.today()

        for mapping, team in results.all():
            oncall = None
            slack_group_id = None

            if team:
                slack_group_id = team.slack_group_id
                # Check for active schedule first
                current = await self.get_current_oncall_for_team(str(team.id), check_date=today)
                if current:
                    oncall = current.get("engineer_slack_id")
                # Fallback to team oncall_engineer
                if not oncall:
                    oncall = team.oncall_engineer

            # Final fallback to service primary_oncall
            if not oncall:
                oncall = mapping.primary_oncall

            # Include service_owner for tagging fallback (oncall_engineer -> service_owner -> slack_group_id)
            service_owner = mapping.service_owner

            # Deduplicate by team or engineer. Always include all three keys (use None when missing)
            # so Slack activity receives them and can apply priority: oncall_engineer > service_owner > slack_group_id.
            key = slack_group_id or oncall or ""
            if key and key not in seen:
                seen.add(key)
                entries.append({
                    "oncall_engineer": oncall,
                    "service_owner": service_owner,
                    "slack_group_id": slack_group_id,
                })

        return entries

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

    async def save_finding(
        self,
        bug_id: str,
        category: str,
        finding: str,
        severity: str,
    ) -> InvestigationFinding:
        entry = InvestigationFinding(
            bug_id=bug_id, category=category, finding=finding, severity=severity
        )
        self.session.add(entry)
        await self.session.commit()
        return entry

    async def get_findings_for_bug(self, bug_id: str) -> list[InvestigationFinding]:
        stmt = (
            select(InvestigationFinding)
            .where(InvestigationFinding.bug_id == bug_id)
            .order_by(InvestigationFinding.created_at)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_recent_reporter_replies(self, bug_id: str, since: datetime) -> int:
        stmt = (
            select(func.count())
            .select_from(BugConversation)
            .where(
                BugConversation.bug_id == bug_id,
                BugConversation.sender_type == "reporter",
                BugConversation.message_type == "reporter_reply",
                BugConversation.created_at >= since,
            )
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    async def get_recent_open_bugs(self, since: datetime) -> list[BugReport]:
        stmt = (
            select(BugReport)
            .where(
                BugReport.created_at >= since,
                BugReport.status.not_in(["resolved"]),
            )
            .order_by(BugReport.created_at.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_stale_open_bugs(self, threshold: datetime) -> list[BugReport]:
        """Open bugs whose last human interaction (or creation date) is before `threshold`.

        Excludes 'resolved' and 'escalated' (SLA workflow owns escalated bugs).
        """
        last_human_sq = (
            select(func.max(BugConversation.created_at))
            .where(
                BugConversation.bug_id == BugReport.bug_id,
                BugConversation.sender_type.in_(["reporter", "developer"]),
            )
            .correlate(BugReport)
            .scalar_subquery()
        )
        stmt = (
            select(BugReport)
            .where(
                BugReport.status.not_in(["resolved", "escalated"]),
                func.coalesce(last_human_sq, BugReport.created_at) < threshold,
            )
            .order_by(BugReport.created_at)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ── On-Call Scheduling ──────────────────────────────────────────────────────

    async def create_oncall_schedule(
        self, team_id: str, data: dict
    ) -> OnCallSchedule:
        schedule = OnCallSchedule(team_id=team_id, **data)
        self.session.add(schedule)
        await self.session.commit()
        await self.session.refresh(schedule)
        return schedule

    async def get_oncall_schedule_by_id(self, id_: str) -> OnCallSchedule | None:
        stmt = select(OnCallSchedule).where(OnCallSchedule.id == id_)  # type: ignore[arg-type]
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_oncall_schedules_by_team(
        self,
        team_id: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[OnCallSchedule], int]:
        stmt: Select = select(OnCallSchedule).where(
            OnCallSchedule.team_id == team_id  # type: ignore[arg-type]
        )
        if start_date:
            stmt = stmt.where(OnCallSchedule.start_date >= start_date)
        if end_date:
            stmt = stmt.where(OnCallSchedule.end_date <= end_date)

        total = await self.session.execute(
            stmt.with_only_columns(func.count()).order_by(None)
        )
        total_count = int(total.scalar_one())

        offset = (page - 1) * page_size
        stmt = stmt.order_by(OnCallSchedule.start_date).offset(offset).limit(page_size)
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total_count

    async def get_upcoming_oncall_schedules(
        self, team_id: str, from_date: date | None = None
    ) -> list[OnCallSchedule]:
        """Get schedules that start on or after from_date (defaults to today)."""
        if from_date is None:
            from_date = date.today()
        stmt = (
            select(OnCallSchedule)
            .where(
                OnCallSchedule.team_id == team_id,  # type: ignore[arg-type]
                OnCallSchedule.start_date >= from_date,
            )
            .order_by(OnCallSchedule.start_date)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_current_oncall_for_team(
        self, team_id: str, check_date: date | None = None
    ) -> dict | None:
        """Get current on-call engineer for a team.

        Checks active schedule first, then falls back to Team.oncall_engineer.
        Returns dict with engineer_slack_id, effective_date, source, schedule_id.
        """
        if check_date is None:
            check_date = date.today()

        # Check for active schedule
        stmt = (
            select(OnCallSchedule)
            .where(
                OnCallSchedule.team_id == team_id,  # type: ignore[arg-type]
                OnCallSchedule.start_date <= check_date,
                OnCallSchedule.end_date >= check_date,
            )
            .order_by(OnCallSchedule.start_date.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        schedule = result.scalar_one_or_none()

        if schedule:
            # For daily schedules, check if today is in days_of_week
            if schedule.schedule_type == "daily" and schedule.days_of_week:
                today_weekday = check_date.weekday()  # 0=Monday, 6=Sunday
                if today_weekday not in schedule.days_of_week:
                    # Not scheduled for today, fall through to team oncall_engineer
                    schedule = None

        if schedule:
            return {
                "engineer_slack_id": schedule.engineer_slack_id,
                "effective_date": schedule.start_date,
                "source": "schedule",
                "schedule_id": str(schedule.id),
            }

        # Fallback to Team.oncall_engineer
        team = await self.get_team_by_id(team_id)
        if team and team.oncall_engineer:
            return {
                "engineer_slack_id": team.oncall_engineer,
                "effective_date": None,
                "source": "manual",
                "schedule_id": None,
            }

        return None

    async def update_oncall_schedule(
        self, id_: str, data: dict
    ) -> OnCallSchedule | None:
        if not data:
            return await self.get_oncall_schedule_by_id(id_)
        stmt = (
            update(OnCallSchedule)
            .where(OnCallSchedule.id == id_)  # type: ignore[arg-type]
            .values(**data, updated_at=datetime.utcnow())
            .returning(OnCallSchedule)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one_or_none()

    async def delete_oncall_schedule(self, id_: str) -> None:
        schedule = await self.get_oncall_schedule_by_id(id_)
        if schedule is None:
            return
        await self.session.delete(schedule)
        await self.session.commit()

    async def check_schedule_overlap(
        self, team_id: str, start_date: date, end_date: date, exclude_id: str | None = None
    ) -> bool:
        """Check if a schedule overlaps with existing schedules for the team."""
        stmt = select(OnCallSchedule).where(
            OnCallSchedule.team_id == team_id,  # type: ignore[arg-type]
            or_(
                and_(
                    OnCallSchedule.start_date <= end_date,
                    OnCallSchedule.end_date >= start_date,
                )
            ),
        )
        if exclude_id:
            stmt = stmt.where(OnCallSchedule.id != exclude_id)  # type: ignore[arg-type]
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    async def log_oncall_change(
        self,
        team_id: str,
        engineer_slack_id: str,
        change_type: str,
        effective_date: date,
        *,
        previous_engineer_slack_id: str | None = None,
        change_reason: str | None = None,
        changed_by: str | None = None,
    ) -> OnCallHistory:
        history = OnCallHistory(
            team_id=team_id,
            engineer_slack_id=engineer_slack_id,
            previous_engineer_slack_id=previous_engineer_slack_id,
            change_type=change_type,
            change_reason=change_reason,
            effective_date=effective_date,
            changed_by=changed_by,
        )
        self.session.add(history)
        await self.session.commit()
        await self.session.refresh(history)
        return history

    async def get_oncall_history(
        self,
        team_id: str,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[OnCallHistory], int]:
        stmt: Select = select(OnCallHistory).where(
            OnCallHistory.team_id == team_id  # type: ignore[arg-type]
        )

        total = await self.session.execute(
            stmt.with_only_columns(func.count()).order_by(None)
        )
        total_count = int(total.scalar_one())

        offset = (page - 1) * page_size
        stmt = (
            stmt.order_by(desc(OnCallHistory.effective_date), desc(OnCallHistory.created_at))
            .offset(offset)
            .limit(page_size)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total_count

    async def get_next_rotation_engineer(
        self, team: Team
    ) -> str | None:
        """Calculate next engineer in rotation based on rotation_type."""
        if not team.rotation_enabled or not team.rotation_type:
            return None

        if team.rotation_type == "round_robin":
            # This will be handled by the rotation service using Slack API
            # Return None here, let the service layer fetch from Slack
            return None
        elif team.rotation_type == "custom_order" and team.rotation_order:
            if not team.rotation_order:
                return None
            current_idx = team.current_rotation_index or 0
            next_idx = (current_idx + 1) % len(team.rotation_order)
            return team.rotation_order[next_idx]

        return None
