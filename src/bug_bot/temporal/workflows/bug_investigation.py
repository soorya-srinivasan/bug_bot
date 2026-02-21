from dataclasses import replace
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from bug_bot.temporal import (
        BugReportInput, ParsedBug, SLATrackingInput,
        WorkflowState, IncomingMessage,
    )
    from bug_bot.temporal.activities.parsing_activity import parse_bug_report
    from bug_bot.temporal.activities.slack_activity import (
        post_slack_message,
        post_investigation_results,
        create_summary_thread,
        escalate_to_humans,
        post_to_summary_thread,
        PostMessageInput,
        PostResultsInput,
        EscalationInput,
    )
    from bug_bot.temporal.activities.database_activity import (
        update_bug_status,
        update_bug_assignee,
        save_investigation_result,
        save_followup_result,
        store_summary_thread_ts,
        log_conversation_event,
        fetch_oncall_for_services,
    )
    from bug_bot.temporal.activities.agent_activity import (
        run_agent_investigation,
        run_continuation_investigation,
        cleanup_workspace,
    )
    from bug_bot.temporal.workflows.sla_tracking import SLATrackingWorkflow

_AGENT_RETRY = RetryPolicy(
    maximum_attempts=2,
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
)


@workflow.defn
class BugInvestigationWorkflow:
    def __init__(self) -> None:
        self._state: WorkflowState = WorkflowState.INVESTIGATING
        self._message_queue: list[IncomingMessage] = []
        self._close_requested: bool = False
        # True once a dev has replied at least once; gates "resolved" auto-close.
        self._dev_replied: bool = False
        # Prevents creating a second summary thread on follow-up iterations.
        self._summary_thread_created: bool = False
        # Guards against double-running cleanup_workspace when the finally block
        # fires after a path that already called it explicitly.
        self._workspace_cleaned: bool = False
        # Prevents starting the SLA child workflow more than once.
        self._sla_tracking_started: bool = False
        # Dev takeover state
        self._dev_takeover: bool = False
        self._takeover_user_id: str | None = None

    @workflow.signal
    async def incoming_message(self, sender_type: str, sender_id: str, conversation_id: str) -> None:
        """Unified signal for all incoming messages. conversation_id is the UUID of the
        already-persisted BugConversation row so the agent can look it up via the tool."""
        self._message_queue.append(
            IncomingMessage(sender_type=sender_type, sender_id=sender_id, conversation_id=conversation_id)
        )

    @workflow.signal
    async def close_requested(self) -> None:
        """Reporter or developer asked to close/cancel the bug."""
        self._close_requested = True

    @workflow.signal
    async def dev_takeover(self, dev_user_id: str) -> None:
        """Dev claimed ownership of the bug; stop further Claude investigations."""
        self._dev_takeover = True
        self._takeover_user_id = dev_user_id

    @workflow.run
    async def run(self, input: BugReportInput) -> dict:
        workflow.logger.info(f"Starting investigation for {input.bug_id}")
        try:
            return await self._run(input)
        finally:
            if not self._workspace_cleaned:
                try:
                    await workflow.execute_activity(
                        cleanup_workspace, args=[input.bug_id],
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                except Exception:
                    workflow.logger.warning(
                        "Cleanup activity failed for %s during teardown", input.bug_id
                    )

    async def _run(self, input: BugReportInput) -> dict:
        parsed: ParsedBug = await workflow.execute_activity(
            parse_bug_report, input,
            start_to_close_timeout=timedelta(seconds=30),
        )

        await workflow.execute_activity(
            update_bug_status, args=[input.bug_id, "investigating"],
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            log_conversation_event,
            args=[input.bug_id, "status_update", "system", None, None, "Status changed to: investigating", None],
            start_to_close_timeout=timedelta(seconds=10),
        )

        investigation_dict: dict = await workflow.execute_activity(
            run_agent_investigation,
            args=[input.bug_id, input.message_text, parsed.severity, parsed.relevant_services, input.attachments],
            start_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(minutes=2),
            retry_policy=_AGENT_RETRY,
        )

        claude_session_id = investigation_dict.get("claude_session_id")
        action = investigation_dict.get("action", "post_findings")

        # ── Main loop ─────────────────────────────────────────────────────────
        while True:
            # ── Dev takeover check (before close check) ───────────────────────
            if self._dev_takeover:
                await self._handle_dev_takeover(input)
                return {"fix_type": "dev_takeover", "bug_id": input.bug_id}

            # ── Close check (reporter or dev sent close signal) ───────────────
            if self._close_requested:
                await self._handle_close(input)
                return {"fix_type": "closed_by_reporter", "bug_id": input.bug_id}

            # ── ask_reporter: need more info before we can post findings ───────
            if action == "ask_reporter":
                questions: list[str] = investigation_dict.get("clarification_questions") or []
                if len(questions) == 1:
                    slack_text = f":speech_balloon: *Bug Bot has a question:*\n{questions[0]}"
                elif questions:
                    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
                    slack_text = f":speech_balloon: *Bug Bot has a few questions:*\n{numbered}"
                else:
                    slack_text = ":speech_balloon: *Bug Bot needs more information to continue the investigation.*"
                log_text = "\n".join(questions)
                await workflow.execute_activity(
                    post_slack_message,
                    PostMessageInput(
                        channel_id=input.channel_id,
                        thread_ts=input.thread_ts,
                        text=slack_text,
                    ),
                    start_to_close_timeout=timedelta(seconds=15),
                )
                await workflow.execute_activity(
                    log_conversation_event,
                    args=[input.bug_id, "clarification_request", "bot", "bugbot",
                          input.channel_id, log_text, None],
                    start_to_close_timeout=timedelta(seconds=10),
                )

                self._state = WorkflowState.AWAITING_REPORTER
                await workflow.wait_condition(
                    lambda: self._close_requested or any(
                        m.sender_type == "reporter" for m in self._message_queue
                    ),
                    timeout=timedelta(hours=2),
                )
                self._state = WorkflowState.INVESTIGATING

                if self._close_requested:
                    await self._handle_close(input)
                    return {"fix_type": "closed_by_reporter", "bug_id": input.bug_id}

                reporter_has_messages = any(m.sender_type == "reporter" for m in self._message_queue)
                if not reporter_has_messages:
                    workflow.logger.info(
                        f"No clarification reply for {input.bug_id} in 2 h — proceeding"
                    )

                conversation_ids = [m.conversation_id for m in self._message_queue]
                self._message_queue.clear()

                investigation_dict = await workflow.execute_activity(
                    run_continuation_investigation,
                    args=[input.bug_id, conversation_ids,
                          WorkflowState.AWAITING_REPORTER.value, claude_session_id],
                    start_to_close_timeout=timedelta(minutes=15),
                    heartbeat_timeout=timedelta(minutes=2),
                    retry_policy=_AGENT_RETRY,
                )
                await workflow.execute_activity(
                    save_followup_result,
                    args=[input.bug_id, WorkflowState.AWAITING_REPORTER.value, investigation_dict],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                claude_session_id = investigation_dict.get("claude_session_id", claude_session_id)
                action = investigation_dict.get("action", "post_findings")
                continue

            # ── Dev follow-up: reply in the existing summary thread ────────
            if self._dev_replied and self._summary_thread_created:
                await workflow.execute_activity(
                    save_followup_result,
                    args=[input.bug_id, WorkflowState.AWAITING_DEV.value, investigation_dict],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                await workflow.execute_activity(
                    log_conversation_event,
                    args=[input.bug_id, "investigation_result", "bot", "bugbot", None,
                          investigation_dict.get("summary"),
                          {"fix_type": investigation_dict.get("fix_type"),
                           "confidence": investigation_dict.get("confidence")}],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                response_text = investigation_dict.get("summary", "Follow-up investigation complete.")
                await workflow.execute_activity(
                    post_to_summary_thread,
                    args=[input.bug_id, f":mag: *Follow-up for {input.bug_id}:*\n{response_text}"],
                    start_to_close_timeout=timedelta(seconds=15),
                )

                if action == "resolved":
                    await workflow.execute_activity(
                        update_bug_status, args=[input.bug_id, "resolved"],
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    await workflow.execute_activity(
                        log_conversation_event,
                        args=[input.bug_id, "resolved", "bot", "bugbot", None,
                              investigation_dict.get("summary", "Resolved after dev review"), None],
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    self._workspace_cleaned = True
                    await workflow.execute_activity(
                        cleanup_workspace, args=[input.bug_id],
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                    return investigation_dict
                # No status change or re-escalation; fall through to wait for dev

            else:
                # ── First investigation: save + post to #bug-reports + create summary
                await workflow.execute_activity(
                    save_investigation_result, args=[input.bug_id, investigation_dict],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                await workflow.execute_activity(
                    log_conversation_event,
                    args=[input.bug_id, "investigation_result", "bot", "bugbot", None,
                          investigation_dict.get("summary"),
                          {"fix_type": investigation_dict.get("fix_type"),
                           "confidence": investigation_dict.get("confidence")}],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                if investigation_dict.get("pr_url"):
                    await workflow.execute_activity(
                        log_conversation_event,
                        args=[input.bug_id, "pr_created", "bot", "bugbot", None,
                              investigation_dict.get("pr_url"), None],
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                results_input = PostResultsInput(
                    channel_id=input.channel_id, thread_ts=input.thread_ts,
                    bug_id=input.bug_id, severity=parsed.severity, result=investigation_dict,
                )
                await workflow.execute_activity(
                    post_investigation_results, results_input,
                    start_to_close_timeout=timedelta(seconds=15),
                )

                if not self._summary_thread_created:
                    # Use agent-detected services if available, otherwise fall back to parsed services
                    relevant_services = investigation_dict.get("relevant_services") or parsed.relevant_services
                    workflow.logger.info(f"Fetching on-call for services: {relevant_services}")
                    oncall_entries: list[dict] = await workflow.execute_activity(
                        fetch_oncall_for_services,
                        args=[relevant_services],
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    summary_input = replace(results_input, oncall_entries=oncall_entries)
                    summary_thread_ts: str = await workflow.execute_activity(
                        create_summary_thread, summary_input,
                        start_to_close_timeout=timedelta(seconds=15),
                    )
                    if summary_thread_ts:
                        await workflow.execute_activity(
                            store_summary_thread_ts, args=[input.bug_id, summary_thread_ts],
                            start_to_close_timeout=timedelta(seconds=10),
                        )
                    self._summary_thread_created = True

                # ── Escalate if needed ───────────────────────────────────────
                if action == "escalate":
                    # Use agent-detected services if available, otherwise fall back to parsed services
                    relevant_services = investigation_dict.get("relevant_services") or parsed.relevant_services
                    workflow.logger.info(f"Escalating - fetching on-call for services: {relevant_services}")
                    oncall_entries: list[dict] = await workflow.execute_activity(
                        fetch_oncall_for_services,
                        args=[relevant_services],
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                    await workflow.execute_activity(
                        escalate_to_humans,
                        EscalationInput(
                            channel_id=input.channel_id, thread_ts=input.thread_ts,
                            bug_id=input.bug_id, severity=parsed.severity,
                            relevant_services=relevant_services,
                            oncall_entries=oncall_entries,
                        ),
                        start_to_close_timeout=timedelta(seconds=15),
                    )
                    if not self._sla_tracking_started:
                        await workflow.start_child_workflow(
                            SLATrackingWorkflow.run,
                            SLATrackingInput(
                                bug_id=input.bug_id, severity=parsed.severity,
                                channel_id=input.channel_id, thread_ts=input.thread_ts,
                                assigned_users=investigation_dict.get("recommended_actions", []),
                            ),
                            id=f"sla-{input.bug_id}",
                        )
                        self._sla_tracking_started = True
                    await workflow.execute_activity(
                        update_bug_status, args=[input.bug_id, "escalated"],
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                else:
                    # post_findings or resolved-before-dev-reply: wait for dev
                    await workflow.execute_activity(
                        update_bug_status, args=[input.bug_id, "pending_verification"],
                        start_to_close_timeout=timedelta(seconds=10),
                    )

            # ── Always wait for dev feedback before looping ───────────────────
            self._state = WorkflowState.AWAITING_DEV
            await workflow.wait_condition(
                lambda: self._close_requested or self._dev_takeover or any(
                    m.sender_type == "developer" for m in self._message_queue
                ),
                timeout=timedelta(hours=48),
            )
            self._state = WorkflowState.INVESTIGATING

            if self._dev_takeover:
                await self._handle_dev_takeover(input)
                return {"fix_type": "dev_takeover", "bug_id": input.bug_id}

            if self._close_requested:
                await self._handle_close(input)
                return {"fix_type": "closed_by_reporter", "bug_id": input.bug_id}

            # Check the actual queue state — wait_condition return value is unreliable
            # across SDK versions and can be None even when the condition was met.
            dev_has_messages = any(m.sender_type == "developer" for m in self._message_queue)
            if not dev_has_messages:
                # 48-hour timeout with no dev reply — auto-resolve
                await workflow.execute_activity(
                    update_bug_status, args=[input.bug_id, "resolved"],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                self._workspace_cleaned = True
                await workflow.execute_activity(
                    cleanup_workspace, args=[input.bug_id],
                    start_to_close_timeout=timedelta(seconds=30),
                )
                return investigation_dict

            # A developer has replied — drain only their messages and continue.
            self._dev_replied = True

            # Drain queue — only the messages since last agent turn are passed
            # to the continuation prompt; the agent fetches full history via tool.
            conversation_ids = [m.conversation_id for m in self._message_queue]
            self._message_queue.clear()

            # await workflow.execute_activity(
            #     post_slack_message,
            #     PostMessageInput(
            #         channel_id=input.channel_id, thread_ts=input.thread_ts,
            #         text=f":mag: Follow-up investigation started for {input.bug_id}...",
            #     ),
            #     start_to_close_timeout=timedelta(seconds=15),
            # )

            investigation_dict = await workflow.execute_activity(
                run_continuation_investigation,
                args=[input.bug_id, conversation_ids,
                      WorkflowState.AWAITING_DEV.value, claude_session_id],
                start_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=_AGENT_RETRY,
            )
            claude_session_id = investigation_dict.get("claude_session_id", claude_session_id)
            action = investigation_dict.get("action", "post_findings")
            # Loop back to re-evaluate the action

    async def _handle_close(self, input: BugReportInput) -> None:
        await workflow.execute_activity(
            update_bug_status, args=[input.bug_id, "resolved"],
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            log_conversation_event,
            args=[input.bug_id, "resolved", "system", None,
                  input.channel_id, "Bug closed on request", None],
            start_to_close_timeout=timedelta(seconds=10),
        )
        self._workspace_cleaned = True
        await workflow.execute_activity(
            cleanup_workspace, args=[input.bug_id],
            start_to_close_timeout=timedelta(seconds=30),
        )

    async def _handle_dev_takeover(self, input: BugReportInput) -> None:
        """Record the assignee, set status to dev_takeover, and wait for a close signal."""
        await workflow.execute_activity(
            update_bug_assignee, args=[input.bug_id, self._takeover_user_id],
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            update_bug_status, args=[input.bug_id, WorkflowState.DEV_TAKEOVER.value],
            start_to_close_timeout=timedelta(seconds=10),
        )
        await workflow.execute_activity(
            log_conversation_event,
            args=[input.bug_id, "dev_takeover", "developer", self._takeover_user_id,
                  None, f"Dev takeover by {self._takeover_user_id}", None],
            start_to_close_timeout=timedelta(seconds=10),
        )

        # Wait indefinitely for a close signal. Incoming dev messages are still
        # logged via the incoming_message signal → DB path, but we don't feed
        # them to Claude. 7-day timeout acts as a safety net.
        self._state = WorkflowState.DEV_TAKEOVER
        await workflow.wait_condition(
            lambda: self._close_requested,
            timeout=timedelta(days=7),
        )

        if self._close_requested:
            await self._handle_close(input)
        else:
            # 7-day safety-net timeout — auto-resolve
            self._workspace_cleaned = True
            await workflow.execute_activity(
                update_bug_status, args=[input.bug_id, "resolved"],
                start_to_close_timeout=timedelta(seconds=10),
            )
            await workflow.execute_activity(
                cleanup_workspace, args=[input.bug_id],
                start_to_close_timeout=timedelta(seconds=30),
            )
