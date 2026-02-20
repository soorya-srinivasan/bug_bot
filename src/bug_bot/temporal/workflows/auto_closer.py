from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from bug_bot.temporal.activities.database_activity import (
        find_stale_bugs,
        mark_bug_auto_closed,
    )
    from bug_bot.temporal.activities.agent_activity import cleanup_workspace
    from bug_bot.temporal.workflows.bug_investigation import BugInvestigationWorkflow


@dataclass
class AutoCloseInput:
    inactivity_days: int = 5


@workflow.defn
class AutoCloseWorkflow:
    """Hourly scheduled workflow — closes stale open bugs."""

    @workflow.run
    async def run(self, input: AutoCloseInput) -> dict:
        stale: list[dict] = await workflow.execute_activity(
            find_stale_bugs,
            args=[input.inactivity_days],
            start_to_close_timeout=timedelta(seconds=30),
        )

        if not stale:
            workflow.logger.info("No stale bugs found")
            return {"closed": 0, "signaled": 0, "direct": 0, "errors": 0}

        workflow.logger.info(f"Auto-closing {len(stale)} stale bugs")
        signaled = direct = errors = 0

        for bug in stale:
            bug_id: str = bug["bug_id"]
            workflow_id: str | None = bug["temporal_workflow_id"]
            try:
                closed_via_signal = False
                if workflow_id:
                    try:
                        handle = workflow.get_external_workflow_handle_for(
                            BugInvestigationWorkflow.run, workflow_id
                        )
                        await handle.signal(BugInvestigationWorkflow.close_requested)
                        signaled += 1
                        closed_via_signal = True
                        workflow.logger.info(f"Signalled close for {bug_id}")
                    except Exception as e:
                        # Workflow already finished or was never running
                        workflow.logger.warning(
                            f"Signal failed for {bug_id} ({type(e).__name__}), using direct path"
                        )

                if not closed_via_signal:
                    await workflow.execute_activity(
                        mark_bug_auto_closed,
                        args=[bug_id],
                        start_to_close_timeout=timedelta(seconds=15),
                    )
                    await workflow.execute_activity(
                        cleanup_workspace,
                        args=[bug_id],
                        start_to_close_timeout=timedelta(seconds=30),
                    )
                    direct += 1
                    workflow.logger.info(f"Directly closed {bug_id}")

            except Exception as e:
                workflow.logger.error(f"Failed to close {bug_id}: {e}")
                errors += 1

        return {"closed": signaled + direct, "signaled": signaled, "direct": direct, "errors": errors}
