from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from bug_bot.temporal import BugReportInput, ParsedBug, InvestigationResult, SLATrackingInput
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
    )
    from bug_bot.temporal.activities.agent_activity import (
        run_agent_investigation,
        run_followup_investigation,
    )
    from bug_bot.temporal.workflows.sla_tracking import SLATrackingWorkflow


@workflow.defn
class BugInvestigationWorkflow:
    def __init__(self) -> None:
        self._dev_reply: dict | None = None

    @workflow.signal
    async def dev_reply(self, message: str, reply_type: str) -> None:
        """Signal handler for developer replies in the summary thread."""
        self._dev_reply = {"message": message, "reply_type": reply_type}

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

        await workflow.execute_activity(
            save_investigation_result, args=[input.bug_id, investigation_dict],
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

        summary_thread_ts: str = await workflow.execute_activity(
            create_summary_thread, results_input,
            start_to_close_timeout=timedelta(seconds=15),
        )
        if summary_thread_ts:
            await workflow.execute_activity(
                store_summary_thread_ts, args=[input.bug_id, summary_thread_ts],
                start_to_close_timeout=timedelta(seconds=10),
            )

        fix_type = investigation_dict.get("fix_type", "unknown")
        if fix_type in ("needs_human", "unknown"):
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
            # ✅ Don't resolve yet — wait for dev confirmation
            await workflow.execute_activity(
                update_bug_status, args=[input.bug_id, "pending_verification"],
                start_to_close_timeout=timedelta(seconds=10),
            )

        # ✅ Loop to support multiple dev replies
        claude_session_id = investigation_dict.get("claude_session_id")
        current_result = investigation_dict

        while True:
            condition_met = await workflow.wait_condition(
                lambda: self._dev_reply is not None,
                timeout=timedelta(hours=48),
            )

            if not condition_met:
                workflow.logger.info(f"No reply received for {input.bug_id}, closing workflow")
                # If still pending verification with no reply, mark resolved
                await workflow.execute_activity(
                    update_bug_status, args=[input.bug_id, "resolved"],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                break

            dev_reply_data = self._dev_reply
            self._dev_reply = None  # Reset for next iteration
            reply_type = dev_reply_data["reply_type"]

            workflow.logger.info(f"Dev replied to {input.bug_id}: reply_type={reply_type}")

            # ✅ Dev explicitly confirmed fix — mark resolved and stop
            if reply_type == "resolved":
                await workflow.execute_activity(
                    update_bug_status, args=[input.bug_id, "resolved"],
                    start_to_close_timeout=timedelta(seconds=10),
                )
                await workflow.execute_activity(
                    post_slack_message,
                    PostMessageInput(
                        channel_id=input.channel_id, thread_ts=input.thread_ts,
                        text=f":white_check_mark: {input.bug_id} marked as resolved by developer.",
                    ),
                    start_to_close_timeout=timedelta(seconds=15),
                )
                break

            # Otherwise run a follow-up investigation
            await workflow.execute_activity(
                post_slack_message,
                PostMessageInput(
                    channel_id=input.channel_id, thread_ts=input.thread_ts,
                    text=f":mag: Follow-up investigation started for {input.bug_id}...",
                ),
                start_to_close_timeout=timedelta(seconds=15),
            )

            followup_result: dict = await workflow.execute_activity(
                run_followup_investigation,
                args=[input.bug_id, dev_reply_data["message"], reply_type, claude_session_id],
                start_to_close_timeout=timedelta(minutes=15),
                heartbeat_timeout=timedelta(minutes=2),
                retry_policy=RetryPolicy(
                    maximum_attempts=2,
                    initial_interval=timedelta(seconds=10),
                    backoff_coefficient=2.0,
                ),
            )

            # Update session ID in case it rotated
            claude_session_id = followup_result.get("claude_session_id", claude_session_id)
            current_result = followup_result

            await workflow.execute_activity(
                save_investigation_result, args=[input.bug_id, followup_result],
                start_to_close_timeout=timedelta(seconds=10),
            )

            followup_results_input = PostResultsInput(
                channel_id=input.channel_id, thread_ts=input.thread_ts,
                bug_id=input.bug_id, severity=parsed.severity, result=followup_result,
            )
            await workflow.execute_activity(
                post_investigation_results, followup_results_input,
                start_to_close_timeout=timedelta(seconds=15),
            )

            # ✅ Update status after each follow-up
            followup_fix_type = followup_result.get("fix_type", "unknown")
            new_status = "pending_verification" if followup_fix_type not in ("needs_human", "unknown") else "escalated"
            await workflow.execute_activity(
                update_bug_status, args=[input.bug_id, new_status],
                start_to_close_timeout=timedelta(seconds=10),
            )

        return current_result