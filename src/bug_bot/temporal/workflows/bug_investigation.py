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
        PostMessageInput,
        PostResultsInput,
        EscalationInput,
    )
    from bug_bot.temporal.activities.database_activity import (
        update_bug_status,
        save_investigation_result,
        store_summary_thread_ts,
        log_conversation_event,
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

    @workflow.run
    async def run(self, input: BugReportInput) -> dict:
        workflow.logger.info(f"Starting investigation for {input.bug_id}")

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
            # ── Close check (reporter or dev sent close signal) ───────────────
            if self._close_requested:
                await self._handle_close(input)
                return {"fix_type": "closed_by_reporter", "bug_id": input.bug_id}

            # ── ask_reporter: need more info before we can post findings ───────
            if action == "ask_reporter":
                clarification_question = investigation_dict.get("clarification_question", "")
                await workflow.execute_activity(
                    post_slack_message,
                    PostMessageInput(
                        channel_id=input.channel_id,
                        thread_ts=input.thread_ts,
                        text=f":speech_balloon: *Bug Bot has a question:*\n{clarification_question}",
                    ),
                    start_to_close_timeout=timedelta(seconds=15),
                )
                await workflow.execute_activity(
                    log_conversation_event,
                    args=[input.bug_id, "clarification_request", "bot", "bugbot",
                          input.channel_id, clarification_question, None],
                    start_to_close_timeout=timedelta(seconds=10),
                )

                self._state = WorkflowState.AWAITING_REPORTER
                reporter_arrived = await workflow.wait_condition(
                    lambda: self._close_requested or any(
                        m.sender_type == "reporter" for m in self._message_queue
                    ),
                    timeout=timedelta(hours=2),
                )
                self._state = WorkflowState.INVESTIGATING

                if self._close_requested:
                    await self._handle_close(input)
                    return {"fix_type": "closed_by_reporter", "bug_id": input.bug_id}

                if not reporter_arrived:
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
                claude_session_id = investigation_dict.get("claude_session_id", claude_session_id)
                action = investigation_dict.get("action", "post_findings")
                continue

            # ── All other actions: save + post results ────────────────────────
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

            # Create the #bug-summaries thread only on the first post; subsequent
            # investigation results are posted into the existing thread by
            # post_investigation_results using the stored summary_thread_ts.
            if not self._summary_thread_created:
                summary_thread_ts: str = await workflow.execute_activity(
                    create_summary_thread, results_input,
                    start_to_close_timeout=timedelta(seconds=15),
                )
                if summary_thread_ts:
                    await workflow.execute_activity(
                        store_summary_thread_ts, args=[input.bug_id, summary_thread_ts],
                        start_to_close_timeout=timedelta(seconds=10),
                    )
                self._summary_thread_created = True

            # ── action == "resolved" is only honoured after a dev has replied ──
            # (means dev asked the agent to close and the agent confirmed it)
            if action == "resolved" and self._dev_replied:
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
                await workflow.execute_activity(
                    cleanup_workspace, args=[input.bug_id],
                    start_to_close_timeout=timedelta(seconds=30),
                )
                return investigation_dict

            # ── Escalate if needed (SLA child runs in parallel; we still wait) ─
            if action == "escalate":
                await workflow.execute_activity(
                    escalate_to_humans,
                    EscalationInput(
                        channel_id=input.channel_id, thread_ts=input.thread_ts,
                        bug_id=input.bug_id, severity=parsed.severity,
                        relevant_services=investigation_dict.get("relevant_services", []),
                    ),
                    start_to_close_timeout=timedelta(seconds=15),
                )
                await workflow.start_child_workflow(
                    SLATrackingWorkflow.run,
                    SLATrackingInput(
                        bug_id=input.bug_id, severity=parsed.severity,
                        channel_id=input.channel_id, thread_ts=input.thread_ts,
                        assigned_users=investigation_dict.get("recommended_actions", []),
                    ),
                    id=f"sla-{input.bug_id}",
                )
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
            dev_arrived = await workflow.wait_condition(
                lambda: self._close_requested or len(self._message_queue) > 0,
                timeout=timedelta(hours=48),
            )
            self._state = WorkflowState.INVESTIGATING

            if self._close_requested:
                await self._handle_close(input)
                return {"fix_type": "closed_by_reporter", "bug_id": input.bug_id}

            if not dev_arrived:
                # 48-hour timeout with no reply — auto-resolve
                await workflow.execute_activity(
                    update_bug_status, args=[input.bug_id, "resolved"],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                await workflow.execute_activity(
                    cleanup_workspace, args=[input.bug_id],
                    start_to_close_timeout=timedelta(seconds=30),
                )
                return investigation_dict

            # Mark that a dev (or anyone) has now replied at least once.
            # From this point, action=="resolved" from the agent means the dev
            # explicitly asked to close and the agent confirmed it.
            self._dev_replied = True

            # Drain queue — only the messages since last agent turn are passed
            # to the continuation prompt; the agent fetches full history via tool.
            conversation_ids = [m.conversation_id for m in self._message_queue]
            self._message_queue.clear()

            await workflow.execute_activity(
                post_slack_message,
                PostMessageInput(
                    channel_id=input.channel_id, thread_ts=input.thread_ts,
                    text=f":mag: Follow-up investigation started for {input.bug_id}...",
                ),
                start_to_close_timeout=timedelta(seconds=15),
            )

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
        await workflow.execute_activity(
            cleanup_workspace, args=[input.bug_id],
            start_to_close_timeout=timedelta(seconds=30),
        )
