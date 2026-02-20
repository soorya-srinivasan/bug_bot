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
    store_summary_thread_ts,
    get_sla_config_for_severity,
    log_conversation_event,
    fetch_oncall_for_services,
)
from bug_bot.temporal.activities.agent_activity import (
    run_agent_investigation,
    run_continuation_investigation,
    cleanup_workspace,
)


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
            store_summary_thread_ts,
            get_sla_config_for_severity,
            run_agent_investigation,
            run_continuation_investigation,
            cleanup_workspace,
            log_conversation_event,
            fetch_oncall_for_services,
        ],
    )

    logging.info(f"Worker started on task queue: {settings.temporal_task_queue}")
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
