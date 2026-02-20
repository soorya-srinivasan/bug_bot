import asyncio
import logging
from datetime import timedelta

from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
)
from temporalio.worker import Worker

from bug_bot.config import settings
from bug_bot.temporal.workflows.auto_closer import AutoCloseInput, AutoCloseWorkflow
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
    update_bug_assignee,
    save_investigation_result,
    store_summary_thread_ts,
    get_sla_config_for_severity,
    log_conversation_event,
    fetch_oncall_for_services,
    find_stale_bugs,
    mark_bug_auto_closed,
)
from bug_bot.temporal.activities.agent_activity import (
    run_agent_investigation,
    run_continuation_investigation,
    cleanup_workspace,
)


SCHEDULE_ID = "auto-close-hourly-schedule"


async def _ensure_auto_close_schedule(client: Client) -> None:
    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            AutoCloseWorkflow.run,
            AutoCloseInput(inactivity_days=settings.auto_close_inactivity_days),
            id="auto-close-hourly",
            task_queue=settings.temporal_task_queue,
        ),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(hours=1))]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
    )
    try:
        await client.create_schedule(SCHEDULE_ID, schedule)
        logging.info("Auto-close schedule created")
    except Exception as e:
        if "already" in str(e).lower():
            logging.info("Auto-close schedule already exists, skipping")
        else:
            raise


async def main():
    logging.basicConfig(level=logging.INFO)

    client = await Client.connect(
        settings.temporal_host,
        namespace=settings.temporal_namespace,
    )

    await _ensure_auto_close_schedule(client)

    worker = Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[
            BugInvestigationWorkflow,
            SLATrackingWorkflow,
            AutoCloseWorkflow,
        ],
        activities=[
            parse_bug_report,
            post_slack_message,
            post_investigation_results,
            create_summary_thread,
            escalate_to_humans,
            send_follow_up,
            update_bug_status,
            update_bug_assignee,
            save_investigation_result,
            store_summary_thread_ts,
            get_sla_config_for_severity,
            run_agent_investigation,
            run_continuation_investigation,
            cleanup_workspace,
            log_conversation_event,
            fetch_oncall_for_services,
            find_stale_bugs,
            mark_bug_auto_closed,
        ],
    )

    logging.info(f"Worker started on task queue: {settings.temporal_task_queue}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
