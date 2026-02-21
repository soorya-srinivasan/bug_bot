from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from bug_bot.temporal.activities.database_activity import (
        fetch_rotation_enabled_teams,
        process_team_rotation,
    )


@workflow.defn
class OnCallRotationWorkflow:
    """Daily scheduled workflow — applies pending on-call rotations for all enabled teams."""

    @workflow.run
    async def run(self) -> dict:
        teams: list[dict] = await workflow.execute_activity(
            fetch_rotation_enabled_teams,
            start_to_close_timeout=timedelta(seconds=30),
        )

        if not teams:
            workflow.logger.info("No rotation-enabled teams found")
            return {"teams": 0, "rotated": 0, "skipped": 0, "errors": 0}

        workflow.logger.info(f"Processing rotation for {len(teams)} teams")
        rotated = skipped = errors = 0

        for team in teams:
            result = await workflow.execute_activity(
                process_team_rotation,
                args=[team["id"]],
                start_to_close_timeout=timedelta(seconds=30),
            )
            if result.get("error"):
                errors += 1
            elif result["rotated"]:
                rotated += 1
            else:
                skipped += 1

        return {"teams": len(teams), "rotated": rotated, "skipped": skipped, "errors": errors}
