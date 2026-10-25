"""Adapter: turn the LinkedIn job manager into the runner's apply_fn.

process_queue takes an injectable ``apply_fn(job, config) -> ApplicationResult``.
This builds the real one around ``job_manager.apply_approved_job``, which returns
a raw ``{status, reason, already_applied, ...}`` outcome. The adapter stamps the
timestamps, classifies the outcome, and assembles the ApplicationResult — so the
browser code never imports the bridge models.
"""

from __future__ import annotations

from typing import Callable

from .outcome_mapping import classify_outcome
from .queue_models import ApplicationResult, utc_now_iso


def make_apply_fn(job_manager, *, allow_external: bool = False, now: Callable[[], str] = utc_now_iso):
    """Return an apply_fn bound to a LinkedIn job manager instance."""

    async def apply_fn(job, config):
        started = now()
        raw = await job_manager.apply_approved_job(
            job, should_submit=config.should_submit, allow_external=allow_external
        )
        finished = now()
        cls = classify_outcome(
            raw.get("status", ""), raw.get("reason", ""), raw.get("already_applied", False)
        )
        return ApplicationResult.for_job(
            job,
            executor_run_id=config.executor_run_id,
            result=cls.result,
            started_at=started,
            finished_at=finished,
            manual_action_required=cls.manual_action_required,
            error_code=cls.error_code,
            error_message=(raw.get("reason") or None) if cls.error_code else None,
            questions_encountered=raw.get("questions", []),
            fields_filled=raw.get("fields_filled", []),
            screenshot_paths=raw.get("screenshots", []),
        )

    return apply_fn
