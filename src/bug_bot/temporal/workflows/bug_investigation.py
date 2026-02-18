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
