from datetime import datetime, date, timedelta, timezone

from sqlalchemy import Select, cast, desc, func, select, text, update, and_, or_, Date
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from bug_bot.models.models import (
    BugReport, BugConversation, BugAuditLog, Investigation, SLAConfig, Escalation,
    ServiceTeamMapping, InvestigationFinding, InvestigationMessage,
    InvestigationFollowup, Team, TeamMembership, OnCallSchedule, OnCallHistory,
    OnCallOverride, OnCallAuditLog,
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
            .values(assignee_user_id=user_id, updated_at=datetime.now(timezone.utc))
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def update_status(self, bug_id: str, status: str) -> None:
        stmt = (
            update(BugReport)
            .where(BugReport.bug_id == bug_id)
            .values(status=status, updated_at=datetime.now(timezone.utc))
        )
        if status == "resolved":
            stmt = stmt.values(resolved_at=datetime.now(timezone.utc))
        await self.session.execute(stmt)
        await self.session.commit()

    async def list_bugs(
        self,
        *,
        bug_id: str | None = None,
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

        if bug_id:
            stmt = stmt.where(BugReport.bug_id.ilike(f"%{bug_id}%"))
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
        resolution_type: str | None = None,
        closure_reason: str | None = None,
        fix_provided: str | None = None,
    ) -> BugReport | None:
        values: dict = {"updated_at": datetime.now(timezone.utc)}
        if severity is not None:
            values["severity"] = severity
        if status is not None:
            values["status"] = status
            if status == "resolved":
                values["resolved_at"] = datetime.now(timezone.utc)
        if resolution_type is not None:
            values["resolution_type"] = resolution_type
        if closure_reason is not None:
            values["closure_reason"] = closure_reason
        if fix_provided is not None:
            values["fix_provided"] = fix_provided

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

    async def update_resolution_details(
        self,
        bug_id: str,
        *,
        resolution_type: str,
        closure_reason: str,
        fix_provided: str | None = None,
    ) -> None:
        values: dict = {
            "resolution_type": resolution_type,
            "closure_reason": closure_reason,
            "updated_at": datetime.now(timezone.utc),
        }
        if fix_provided is not None:
            values["fix_provided"] = fix_provided
        stmt = (
            update(BugReport)
            .where(BugReport.bug_id == bug_id)
            .values(**values)
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def has_pending_closure_request(self, bug_id: str) -> bool:
        stmt = (
            select(func.count())
            .select_from(BugConversation)
            .where(
                BugConversation.bug_id == bug_id,
                BugConversation.message_type == "closure_details_requested",
            )
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one()) > 0

    async def save_investigation(self, bug_id: str, result: dict) -> Investigation:
        conversation_history = result.get("conversation_history")
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
            summary_thread_ts=result.get("summary_thread_ts"),
            claude_session_id=result.get("claude_session_id"),
        )
        self.session.add(investigation)
        await self.session.flush()

        self._bulk_insert_messages(
            bug_id, conversation_history,
            investigation_id=investigation.id,
        )

        await self.session.commit()
        return investigation

    def _bulk_insert_messages(
        self,
        bug_id: str,
        conversation_history: list[dict] | None,
        *,
        investigation_id=None,
        followup_id=None,
    ) -> None:
        if not conversation_history:
            return
        messages = []
        seq = 0
        for msg in conversation_history:
            content = msg.get("text")
            if not content or not content.strip():
                continue
            messages.append(InvestigationMessage(
                bug_id=bug_id,
                investigation_id=investigation_id,
                followup_id=followup_id,
                sequence=seq,
                message_type=msg.get("type", "unknown"),
                content=content,
            ))
            seq += 1
        if messages:
            self.session.add_all(messages)

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
            .values(**data, updated_at=datetime.now(timezone.utc))
            .returning(SLAConfig)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one_or_none()

    async def delete_sla_config(self, id_: str) -> None:
        stmt = (
            update(SLAConfig)
            .where(SLAConfig.id == id_)  # type: ignore[arg-type]
            .values(is_active=False, updated_at=datetime.now(timezone.utc))
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
        is_active: bool | None = True,
        team_id: str | None = None,
        tier: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[ServiceTeamMapping], int]:
        stmt: Select = select(ServiceTeamMapping)
        if service_name:
            stmt = stmt.where(ServiceTeamMapping.service_name.ilike(f"%{service_name}%"))
        if tech_stack:
            stmt = stmt.where(ServiceTeamMapping.tech_stack == tech_stack)
        if is_active is not None:
            stmt = stmt.where(ServiceTeamMapping.is_active == is_active)
        if team_id:
            stmt = stmt.where(ServiceTeamMapping.team_id == team_id)  # type: ignore[arg-type]
        if tier:
            stmt = stmt.where(ServiceTeamMapping.tier == tier)

        total = await self.session.execute(
            stmt.with_only_columns(func.count()).order_by(None)
        )
        total_count = int(total.scalar_one())

        offset = (page - 1) * page_size
        stmt = (
            stmt.options(selectinload(ServiceTeamMapping.team))
            .order_by(ServiceTeamMapping.service_name)
            .offset(offset)
            .limit(page_size)
        )
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
        """Soft delete a service mapping by setting is_active=False."""
        stmt = (
            update(ServiceTeamMapping)
            .where(ServiceTeamMapping.id == id_)  # type: ignore[arg-type]
            .values(is_active=False)
        )
        await self.session.execute(stmt)
        await self.session.commit()

    # ── Team CRUD ───────────────────────────────────────────────────────────────

    @staticmethod
    def _generate_slug(name: str) -> str:
        """Generate a URL-safe slug from a name."""
        import re
        slug = name.lower().strip()
        slug = re.sub(r'[^a-z0-9]+', '-', slug)
        return slug.strip('-')

    async def create_team(self, data: dict) -> Team:
        # Auto-generate name from slack_group_id if not provided
        if "name" not in data or not data["name"]:
            data["name"] = data.get("slack_group_id", "unnamed")
        # Auto-generate slug from name if not provided
        if "slug" not in data or not data["slug"]:
            data["slug"] = self._generate_slug(data["name"])
        team = Team(**data)
        self.session.add(team)
        await self.session.commit()
        await self.session.refresh(team)
        return team

    async def get_team_by_id(self, id_: str) -> Team | None:
        stmt = select(Team).where(Team.id == id_)  # type: ignore[arg-type]
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_team_by_slug(self, slug: str) -> Team | None:
        stmt = select(Team).where(Team.slug == slug)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def list_teams(
        self, *, is_active: bool = True, page: int = 1, page_size: int = 50
    ) -> tuple[list[Team], int]:
        stmt: Select = select(Team)
        if is_active is not None:
            stmt = stmt.where(Team.is_active == is_active)
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
        # If name changed, regenerate slug unless slug is explicitly provided
        if "name" in data and "slug" not in data:
            data["slug"] = self._generate_slug(data["name"])
        stmt = (
            update(Team)
            .where(Team.id == id_)  # type: ignore[arg-type]
            .values(**data, updated_at=datetime.now(timezone.utc))
            .returning(Team)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one_or_none()

    async def delete_team(self, id_: str) -> None:
        """Soft delete a team by setting is_active=False."""
        stmt = (
            update(Team)
            .where(Team.id == id_)  # type: ignore[arg-type]
            .values(is_active=False, updated_at=datetime.now(timezone.utc))
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def get_oncall_for_services(
        self, service_names: list[str], check_date: date | None = None
    ) -> list[dict]:
        """Return deduped on-call info for given services.

        Checks active schedule first, then team oncall_engineer, then service primary_oncall.
        When check_date is provided, resolves on-call as of that date (for historical tagged_on).
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
        resolved_date = check_date if check_date is not None else date.today()

        for mapping, team in results.all():
            oncall = None
            slack_group_id = None

            if team:
                slack_group_id = team.slack_group_id
                # Check for active schedule first
                current = await self.get_current_oncall_for_team(
                    str(team.id), check_date=resolved_date
                )
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

    async def create_audit_log(
        self,
        bug_id: str,
        action: str,
        source: str,
        *,
        performed_by: str | None = None,
        payload: dict | None = None,
        metadata: dict | None = None,
    ) -> BugAuditLog:
        entry = BugAuditLog(
            bug_id=bug_id, action=action, source=source,
            performed_by=performed_by, payload=payload, metadata_=metadata,
        )
        self.session.add(entry)
        await self.session.commit()
        return entry

    async def get_audit_logs(self, bug_id: str) -> list[BugAuditLog]:
        stmt = select(BugAuditLog).where(BugAuditLog.bug_id == bug_id).order_by(BugAuditLog.created_at)
        result = await self.session.execute(stmt)
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

    async def save_followup_investigation(
        self, bug_id: str, trigger_state: str, result: dict
    ) -> InvestigationFollowup:
        conversation_history = result.get("conversation_history")
        followup = InvestigationFollowup(
            bug_id=bug_id,
            trigger_state=trigger_state,
            action=result.get("action", "post_findings"),
            fix_type=result.get("fix_type", "unknown"),
            summary=result.get("summary", ""),
            confidence=result.get("confidence", 0.0),
            root_cause=result.get("root_cause"),
            pr_url=result.get("pr_url"),
            recommended_actions=result.get("recommended_actions", []),
            relevant_services=result.get("relevant_services", []),
            cost_usd=result.get("cost_usd"),
            duration_ms=result.get("duration_ms"),
        )
        self.session.add(followup)
        await self.session.flush()

        self._bulk_insert_messages(
            bug_id, conversation_history,
            followup_id=followup.id,
        )

        await self.session.commit()
        return followup

    async def get_followup_investigations(self, bug_id: str) -> list[InvestigationFollowup]:
        stmt = (
            select(InvestigationFollowup)
            .where(InvestigationFollowup.bug_id == bug_id)
            .order_by(InvestigationFollowup.created_at)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_investigation_messages(
        self,
        bug_id: str,
        *,
        investigation_id: str | None = None,
        followup_id: str | None = None,
    ) -> list[InvestigationMessage]:
        stmt = (
            select(InvestigationMessage)
            .where(InvestigationMessage.bug_id == bug_id)
        )
        if investigation_id is not None:
            stmt = stmt.where(InvestigationMessage.investigation_id == investigation_id)
        if followup_id is not None:
            stmt = stmt.where(InvestigationMessage.followup_id == followup_id)
        stmt = stmt.order_by(InvestigationMessage.sequence)
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

        Priority: Override -> Schedule -> Team.oncall_engineer.
        Returns dict with engineer_slack_id, effective_date, source, schedule_id.
        """
        if check_date is None:
            check_date = date.today()

        # 1. Check for active override (highest priority)
        override = await self.get_active_override_for_team(team_id, check_date)
        if override:
            return {
                "engineer_slack_id": override.substitute_engineer_slack_id,
                "effective_date": override.override_date,
                "source": "override",
                "schedule_id": None,
            }

        # 2. Check for active schedule
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
            .values(**data, updated_at=datetime.now(timezone.utc))
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
        # Write to legacy oncall_history table
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

        # Dual-write to oncall_audit_logs
        change_type_to_action = {
            "manual": "updated",
            "auto_rotation": "rotation_triggered",
            "schedule_created": "created",
            "schedule_updated": "updated",
            "schedule_deleted": "deleted",
            "override_created": "created",
            "override_deleted": "deleted",
        }
        change_type_to_entity = {
            "manual": "team",
            "auto_rotation": "team",
            "schedule_created": "schedule",
            "schedule_updated": "schedule",
            "schedule_deleted": "schedule",
            "override_created": "override",
            "override_deleted": "override",
        }
        audit_entry = OnCallAuditLog(
            team_id=team_id,
            entity_type=change_type_to_entity.get(change_type, "team"),
            entity_id=team_id,
            action=change_type_to_action.get(change_type, change_type),
            actor_type="user" if changed_by else "system",
            actor_id=changed_by,
            engineer_slack_id=engineer_slack_id,
            previous_engineer_slack_id=previous_engineer_slack_id,
            change_type=change_type,
            change_reason=change_reason,
            effective_date=effective_date,
        )
        self.session.add(audit_entry)

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

    async def get_rotation_enabled_teams(self) -> list[Team]:
        """Return all active teams that have rotation enabled."""
        stmt = select(Team).where(Team.rotation_enabled == True, Team.is_active == True)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    # ── On-Call Overrides ──────────────────────────────────────────────────────

    async def get_active_override_for_team(
        self, team_id: str, check_date: date | None = None
    ) -> OnCallOverride | None:
        """Get active override for a team on a specific date. Only considers approved overrides."""
        if check_date is None:
            check_date = date.today()
        stmt = (
            select(OnCallOverride)
            .where(
                OnCallOverride.team_id == team_id,  # type: ignore[arg-type]
                OnCallOverride.status == "approved",
                OnCallOverride.override_date <= check_date,
                or_(
                    # Single-day override: end_date is NULL and override_date matches
                    and_(
                        OnCallOverride.end_date.is_(None),
                        OnCallOverride.override_date == check_date,
                    ),
                    # Multi-day override: end_date >= check_date
                    OnCallOverride.end_date >= check_date,
                ),
            )
            .order_by(desc(OnCallOverride.created_at))
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_oncall_override(
        self, team_id: str, data: dict
    ) -> OnCallOverride:
        override = OnCallOverride(team_id=team_id, **data)
        self.session.add(override)
        await self.session.commit()
        await self.session.refresh(override)
        return override

    async def list_oncall_overrides(
        self,
        team_id: str,
        *,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[OnCallOverride], int]:
        stmt: Select = select(OnCallOverride).where(
            OnCallOverride.team_id == team_id  # type: ignore[arg-type]
        )
        total = await self.session.execute(
            stmt.with_only_columns(func.count()).order_by(None)
        )
        total_count = int(total.scalar_one())
        offset = (page - 1) * page_size
        stmt = (
            stmt.order_by(desc(OnCallOverride.override_date))
            .offset(offset)
            .limit(page_size)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total_count

    async def get_oncall_override_by_id(self, id_: str) -> OnCallOverride | None:
        stmt = select(OnCallOverride).where(OnCallOverride.id == id_)  # type: ignore[arg-type]
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def delete_oncall_override(self, id_: str) -> OnCallOverride | None:
        override = await self.get_oncall_override_by_id(id_)
        if override is None:
            return None
        await self.session.delete(override)
        await self.session.commit()
        return override

    async def check_override_overlap(
        self,
        team_id: str,
        override_date: date,
        end_date: date | None = None,
        exclude_id: str | None = None,
    ) -> bool:
        """Check if an override overlaps with existing non-cancelled/rejected overrides for the team."""
        effective_end = end_date if end_date is not None else override_date
        stmt = select(OnCallOverride).where(
            OnCallOverride.team_id == team_id,  # type: ignore[arg-type]
            OnCallOverride.status.in_(["pending", "approved"]),
            or_(
                # Existing single-day overlaps with new range
                and_(
                    OnCallOverride.end_date.is_(None),
                    OnCallOverride.override_date >= override_date,
                    OnCallOverride.override_date <= effective_end,
                ),
                # Existing multi-day overlaps with new range
                and_(
                    OnCallOverride.end_date.is_not(None),
                    OnCallOverride.override_date <= effective_end,
                    OnCallOverride.end_date >= override_date,
                ),
            ),
        )
        if exclude_id:
            stmt = stmt.where(OnCallOverride.id != exclude_id)  # type: ignore[arg-type]
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none() is not None

    # ── Dashboard Analytics ──────────────────────────────────────────────────

    async def get_dashboard_stats(self) -> dict:
        # Total / open / resolved counts
        total_q = await self.session.execute(
            select(func.count()).select_from(BugReport)
        )
        total_bugs = int(total_q.scalar_one())

        resolved_q = await self.session.execute(
            select(func.count()).select_from(BugReport).where(BugReport.status == "resolved")
        )
        resolved_bugs = int(resolved_q.scalar_one())
        open_bugs = total_bugs - resolved_bugs

        # Average resolution time (hours) for resolved bugs.
        # Uses abs() to handle clock skew between DB server_default now() and Python utcnow.
        avg_res_q = await self.session.execute(
            select(
                func.avg(
                    func.abs(func.extract("epoch", BugReport.resolved_at - BugReport.created_at)) / 3600
                )
            ).where(BugReport.resolved_at.is_not(None))
        )
        avg_resolution_hours = avg_res_q.scalar_one()
        if avg_resolution_hours is not None:
            avg_resolution_hours = round(float(avg_resolution_hours), 2)

        # Escalation rate
        esc_q = await self.session.execute(
            select(func.count(func.distinct(Escalation.bug_id))).select_from(Escalation)
        )
        escalated_count = int(esc_q.scalar_one())
        escalation_rate = round((escalated_count / total_bugs * 100) if total_bugs else 0.0, 1)

        # Investigation aggregate metrics
        inv_agg_q = await self.session.execute(
            select(
                func.avg(Investigation.confidence),
                func.coalesce(func.sum(Investigation.cost_usd), 0.0),
                func.avg(Investigation.duration_ms),
            ).select_from(Investigation)
        )
        inv_row = inv_agg_q.one()
        avg_confidence = round(float(inv_row[0]), 2) if inv_row[0] is not None else None
        total_cost = round(float(inv_row[1]), 2)
        avg_duration = round(float(inv_row[2]), 0) if inv_row[2] is not None else None

        # Bugs by status
        status_q = await self.session.execute(
            select(BugReport.status, func.count())
            .group_by(BugReport.status)
            .order_by(func.count().desc())
        )
        bugs_by_status = [{"status": r[0], "count": r[1]} for r in status_q.all()]

        # Bugs by severity
        sev_q = await self.session.execute(
            select(BugReport.severity, func.count())
            .group_by(BugReport.severity)
            .order_by(BugReport.severity)
        )
        bugs_by_severity = [{"severity": r[0], "count": r[1]} for r in sev_q.all()]

        # Bug trend (last 30 days)
        since = datetime.now(timezone.utc) - timedelta(days=30)
        created_q = await self.session.execute(
            select(
                cast(BugReport.created_at, Date).label("d"),
                func.count(),
            )
            .where(BugReport.created_at >= since)
            .group_by("d")
            .order_by("d")
        )
        created_map: dict[date, int] = {r[0]: r[1] for r in created_q.all()}

        resolved_trend_q = await self.session.execute(
            select(
                cast(BugReport.resolved_at, Date).label("d"),
                func.count(),
            )
            .where(BugReport.resolved_at >= since)
            .group_by("d")
            .order_by("d")
        )
        resolved_map: dict[date, int] = {r[0]: r[1] for r in resolved_trend_q.all()}

        all_dates = sorted(set(created_map.keys()) | set(resolved_map.keys()))
        bug_trend = [
            {
                "date": d.isoformat(),
                "created": created_map.get(d, 0),
                "resolved": resolved_map.get(d, 0),
            }
            for d in all_dates
        ]

        # Average resolution by severity
        avg_sev_q = await self.session.execute(
            select(
                BugReport.severity,
                func.avg(
                    func.abs(func.extract("epoch", BugReport.resolved_at - BugReport.created_at)) / 3600
                ),
            )
            .where(BugReport.resolved_at.is_not(None))
            .group_by(BugReport.severity)
            .order_by(BugReport.severity)
        )
        avg_resolution_by_severity = [
            {"severity": r[0], "avg_hours": round(float(r[1]), 2)}
            for r in avg_sev_q.all()
        ]

        # Fix type distribution
        fix_q = await self.session.execute(
            select(Investigation.fix_type, func.count())
            .group_by(Investigation.fix_type)
            .order_by(func.count().desc())
        )
        fix_type_distribution = [{"fix_type": r[0], "count": r[1]} for r in fix_q.all()]

        # Top affected services (unnest JSONB array)
        svc_q = await self.session.execute(
            text(
                "SELECT svc, COUNT(*) as cnt "
                "FROM investigations, jsonb_array_elements_text(relevant_services) AS svc "
                "GROUP BY svc ORDER BY cnt DESC LIMIT 10"
            )
        )
        top_services = [{"service": r[0], "count": r[1]} for r in svc_q.all()]

        # Findings by category
        cat_q = await self.session.execute(
            select(InvestigationFinding.category, func.count())
            .group_by(InvestigationFinding.category)
            .order_by(func.count().desc())
        )
        findings_by_category = [{"category": r[0], "count": r[1]} for r in cat_q.all()]

        # Findings by severity
        fsev_q = await self.session.execute(
            select(InvestigationFinding.severity, func.count())
            .group_by(InvestigationFinding.severity)
            .order_by(func.count().desc())
        )
        findings_by_severity = [{"severity": r[0], "count": r[1]} for r in fsev_q.all()]

        # Recent bugs (last 10)
        recent_q = await self.session.execute(
            select(BugReport)
            .order_by(BugReport.created_at.desc())
            .limit(10)
        )
        recent_bugs = [
            {
                "bug_id": b.bug_id,
                "severity": b.severity,
                "status": b.status,
                "original_message": b.original_message[:120],
                "created_at": b.created_at.isoformat(),
            }
            for b in recent_q.scalars().all()
        ]

        return {
            "total_bugs": total_bugs,
            "open_bugs": open_bugs,
            "resolved_bugs": resolved_bugs,
            "avg_resolution_hours": avg_resolution_hours,
            "escalation_rate": escalation_rate,
            "avg_confidence": avg_confidence,
            "total_investigation_cost_usd": total_cost,
            "avg_investigation_duration_ms": avg_duration,
            "bugs_by_status": bugs_by_status,
            "bugs_by_severity": bugs_by_severity,
            "bug_trend": bug_trend,
            "avg_resolution_by_severity": avg_resolution_by_severity,
            "fix_type_distribution": fix_type_distribution,
            "top_services": top_services,
            "findings_by_category": findings_by_category,
            "findings_by_severity": findings_by_severity,
            "recent_bugs": recent_bugs,
        }

    # ── Team Membership CRUD ──────────────────────────────────────────────────

    async def list_team_memberships(self, team_id: str) -> list[TeamMembership]:
        stmt = (
            select(TeamMembership)
            .where(TeamMembership.team_id == team_id)  # type: ignore[arg-type]
            .order_by(TeamMembership.joined_at)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def upsert_team_membership(
        self, team_id: str, slack_user_id: str, data: dict
    ) -> TeamMembership:
        stmt = select(TeamMembership).where(
            TeamMembership.team_id == team_id,  # type: ignore[arg-type]
            TeamMembership.slack_user_id == slack_user_id,
        )
        result = await self.session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            for k, v in data.items():
                if v is not None:
                    setattr(existing, k, v)
            await self.session.commit()
            await self.session.refresh(existing)
            return existing
        membership = TeamMembership(
            team_id=team_id, slack_user_id=slack_user_id, **data
        )
        self.session.add(membership)
        await self.session.commit()
        await self.session.refresh(membership)
        return membership

    async def delete_team_membership(self, team_id: str, slack_user_id: str) -> None:
        stmt = select(TeamMembership).where(
            TeamMembership.team_id == team_id,  # type: ignore[arg-type]
            TeamMembership.slack_user_id == slack_user_id,
        )
        result = await self.session.execute(stmt)
        existing = result.scalar_one_or_none()
        if existing:
            await self.session.delete(existing)
            await self.session.commit()

    async def get_eligible_members_for_rotation(
        self, team_id: str
    ) -> list[TeamMembership]:
        stmt = (
            select(TeamMembership)
            .where(
                TeamMembership.team_id == team_id,  # type: ignore[arg-type]
                TeamMembership.is_eligible_for_oncall == True,
            )
            .order_by(TeamMembership.joined_at)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def merge_slack_members_with_db(
        self, team_id: str, slack_user_ids: list[str]
    ) -> list[dict]:
        """Merge Slack group members with DB metadata. Members in Slack but not DB get defaults."""
        db_memberships = await self.list_team_memberships(team_id)
        db_map = {m.slack_user_id: m for m in db_memberships}
        merged = []
        for uid in slack_user_ids:
            if uid in db_map:
                m = db_map[uid]
                merged.append({
                    "id": str(m.id),
                    "slack_user_id": m.slack_user_id,
                    "team_role": m.team_role,
                    "is_eligible_for_oncall": m.is_eligible_for_oncall,
                    "weight": m.weight,
                    "joined_at": m.joined_at.isoformat() if m.joined_at else None,
                    "in_db": True,
                })
            else:
                merged.append({
                    "id": None,
                    "slack_user_id": uid,
                    "team_role": "member",
                    "is_eligible_for_oncall": True,
                    "weight": 1.0,
                    "joined_at": None,
                    "in_db": False,
                })
        return merged

    # ── Override Status Transitions ───────────────────────────────────────────

    async def update_oncall_override(
        self, override_id: str, data: dict
    ) -> OnCallOverride | None:
        if not data:
            return await self.get_oncall_override_by_id(override_id)
        stmt = (
            update(OnCallOverride)
            .where(OnCallOverride.id == override_id)  # type: ignore[arg-type]
            .values(**data)
            .returning(OnCallOverride)
        )
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.scalar_one_or_none()

    # ── OnCall Audit Log ──────────────────────────────────────────────────────

    async def create_oncall_audit_log(
        self,
        entity_type: str,
        entity_id: str,
        action: str,
        *,
        actor_type: str = "user",
        actor_id: str | None = None,
        changes: dict | None = None,
        metadata: dict | None = None,
        team_id: str | None = None,
        engineer_slack_id: str | None = None,
        previous_engineer_slack_id: str | None = None,
        change_type: str | None = None,
        change_reason: str | None = None,
        effective_date: date | None = None,
    ) -> OnCallAuditLog:
        entry = OnCallAuditLog(
            team_id=team_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id,
            changes=changes,
            metadata_=metadata,
            engineer_slack_id=engineer_slack_id,
            previous_engineer_slack_id=previous_engineer_slack_id,
            change_type=change_type,
            change_reason=change_reason,
            effective_date=effective_date,
        )
        self.session.add(entry)
        await self.session.commit()
        await self.session.refresh(entry)
        return entry

    async def list_oncall_audit_logs(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        action: str | None = None,
        actor_id: str | None = None,
        team_id: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[list[OnCallAuditLog], int]:
        stmt: Select = select(OnCallAuditLog)
        if entity_type:
            stmt = stmt.where(OnCallAuditLog.entity_type == entity_type)
        if entity_id:
            stmt = stmt.where(OnCallAuditLog.entity_id == entity_id)  # type: ignore[arg-type]
        if action:
            stmt = stmt.where(OnCallAuditLog.action == action)
        if actor_id:
            stmt = stmt.where(OnCallAuditLog.actor_id == actor_id)
        if team_id:
            stmt = stmt.where(OnCallAuditLog.team_id == team_id)  # type: ignore[arg-type]
        if from_date:
            stmt = stmt.where(cast(OnCallAuditLog.created_at, Date) >= from_date)
        if to_date:
            stmt = stmt.where(cast(OnCallAuditLog.created_at, Date) <= to_date)

        total = await self.session.execute(
            stmt.with_only_columns(func.count()).order_by(None)
        )
        total_count = int(total.scalar_one())

        offset = (page - 1) * page_size
        stmt = (
            stmt.order_by(desc(OnCallAuditLog.created_at))
            .offset(offset)
            .limit(page_size)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all()), total_count

    # ── Schedule helpers ──────────────────────────────────────────────────────

    async def delete_future_auto_schedules(
        self, team_id: str, from_date: date | None = None
    ) -> int:
        """Delete origin='auto' schedules with start_date >= from_date. Returns count deleted."""
        if from_date is None:
            from_date = date.today()
        stmt = (
            select(OnCallSchedule)
            .where(
                OnCallSchedule.team_id == team_id,  # type: ignore[arg-type]
                OnCallSchedule.origin == "auto",
                OnCallSchedule.start_date >= from_date,
            )
        )
        result = await self.session.execute(stmt)
        schedules = list(result.scalars().all())
        for s in schedules:
            await self.session.delete(s)
        if schedules:
            await self.session.commit()
        return len(schedules)

    async def get_user_schedules(
        self, slack_user_id: str, from_date: date | None = None, to_date: date | None = None
    ) -> list[OnCallSchedule]:
        """Get all schedules across teams for a given user."""
        stmt = (
            select(OnCallSchedule)
            .where(OnCallSchedule.engineer_slack_id == slack_user_id)
        )
        if from_date:
            stmt = stmt.where(OnCallSchedule.end_date >= from_date)
        if to_date:
            stmt = stmt.where(OnCallSchedule.start_date <= to_date)
        stmt = stmt.order_by(OnCallSchedule.start_date)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_service_oncall(self, service_id: str) -> dict | None:
        """Get current on-call for a specific service."""
        mapping = await self.get_service_mapping_by_id(service_id)
        if not mapping or not mapping.team_id:
            if mapping and mapping.primary_oncall:
                return {
                    "engineer_slack_id": mapping.primary_oncall,
                    "team_id": None,
                    "team_name": None,
                    "service_name": mapping.service_name,
                    "source": "service_primary",
                }
            return None
        current = await self.get_current_oncall_for_team(str(mapping.team_id))
        if not current:
            return None
        team = await self.get_team_by_id(str(mapping.team_id))
        return {
            "engineer_slack_id": current.get("engineer_slack_id"),
            "team_id": str(mapping.team_id),
            "team_name": team.name if team else None,
            "service_name": mapping.service_name,
            "source": current.get("source"),
        }

    async def global_oncall_lookup(
        self, *, service_name: str | None = None, team_name: str | None = None
    ) -> list[dict]:
        """Convenience lookup: who is on-call for a service or team?"""
        results = []
        if service_name:
            mapping = await self.get_service_mapping(service_name)
            if mapping:
                oncall = await self.get_service_oncall(str(mapping.id))
                if oncall:
                    results.append(oncall)
        if team_name:
            # Try slug first, then name
            team = await self.get_team_by_slug(team_name)
            if not team:
                stmt = select(Team).where(Team.name.ilike(f"%{team_name}%"), Team.is_active == True)
                result = await self.session.execute(stmt)
                team = result.scalar_one_or_none()
            if team:
                current = await self.get_current_oncall_for_team(str(team.id))
                if current:
                    results.append({
                        "engineer_slack_id": current.get("engineer_slack_id"),
                        "team_id": str(team.id),
                        "team_name": team.name,
                        "service_name": None,
                        "source": current.get("source"),
                    })
        return results

    async def get_shift_counts_for_team(self, team_id: str) -> dict[str, int]:
        """Count completed oncall schedules per engineer for a team (for weighted rotation)."""
        stmt = (
            select(
                OnCallSchedule.engineer_slack_id,
                func.count().label("cnt"),
            )
            .where(
                OnCallSchedule.team_id == team_id,  # type: ignore[arg-type]
                OnCallSchedule.end_date < date.today(),
            )
            .group_by(OnCallSchedule.engineer_slack_id)
        )
        result = await self.session.execute(stmt)
        return {row[0]: row[1] for row in result.all()}
