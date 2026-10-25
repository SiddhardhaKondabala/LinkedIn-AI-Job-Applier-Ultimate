"""Queue-mode executor.

Processes a career-ops approved queue sequentially WITHOUT any independent
search or scoring: it never calls SearchCustomizer, never paginates, and never
uses the LLM "is this interesting?" gate. The actual application is delegated to
an injectable ``apply_fn`` (the real one wraps the LinkedIn job manager; tests
pass a mock), so this orchestration is fully unit-testable without a browser.

Per job, in order: enforce the queue's min score, require a valid LinkedIn URL,
require the tailored resume to exist, honor pause/stop between jobs, and record
exactly one ApplicationResult — including for skips and failures.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .executor_config import QueueExecutionConfig
from .queue_models import ApplicationResult, ApprovedJob, utc_now_iso

ApplyFn = Callable[[ApprovedJob, QueueExecutionConfig], Awaitable[ApplicationResult]]

_LINKEDIN_JOB_RE = re.compile(
    r"^https?://([a-z0-9-]+\.)*linkedin\.com/(?:jobs/view/|comm/jobs/view/)\d+", re.I
)


def is_valid_linkedin_url(url: Optional[str]) -> bool:
    """True when the URL points at one LinkedIn job posting."""
    return bool(url and _LINKEDIN_JOB_RE.match(url))


def _result(job, config, result, started, finished, **kw) -> ApplicationResult:
    return ApplicationResult.for_job(
        job,
        executor_run_id=config.executor_run_id,
        result=result,
        started_at=started,
        finished_at=finished,
        **kw,
    )


async def process_queue(
    jobs: list[ApprovedJob],
    apply_fn: ApplyFn,
    config: QueueExecutionConfig,
    *,
    pause_checker: Optional[Callable[[], Awaitable[None]]] = None,
    on_result: Optional[Callable[[ApplicationResult], None]] = None,
    now: Callable[[], str] = utc_now_iso,
    resume_exists: Callable[[str], bool] = lambda p: Path(p).exists(),
    reraise: tuple = (),
) -> list[ApplicationResult]:
    """Run the queue and return one ApplicationResult per job considered.

    on_result, when given, is called with each result the moment it is produced
    (before the next job), so a durable writer can persist it incrementally and
    an interruption still leaves records for the finished jobs.
    """
    results: list[ApplicationResult] = []
    attempted = 0

    def record(res: ApplicationResult) -> None:
        results.append(res)
        if on_result is not None:
            on_result(res)

    for job in jobs:
        if config.max_jobs is not None and attempted >= config.max_jobs:
            break
        if pause_checker is not None:
            await pause_checker()

        if job.career_ops_score < config.min_score:
            record(
                _result(
                    job, config, "skipped", now(), now(),
                    error_code="below_min_score",
                    error_message=f"score {job.career_ops_score} < min {config.min_score}",
                )
            )
            continue

        if not is_valid_linkedin_url(job.canonical_url):
            record(
                _result(
                    job, config, "invalid", now(), now(),
                    error_code="invalid_linkedin_url",
                    error_message=job.canonical_url,
                    manual_action_required=True,
                )
            )
            continue

        if not resume_exists(job.resume_path):
            record(
                _result(
                    job, config, "invalid", now(), now(),
                    error_code="missing_resume",
                    error_message=job.resume_path,
                    manual_action_required=True,
                )
            )
            continue

        if config.dry_run:
            record(
                _result(
                    job, config, "skipped", now(), now(),
                    error_code="dry_run",
                    error_message="dry-run: queue validated, not executed",
                )
            )
            continue

        started = now()
        try:
            res = await apply_fn(job, config)
        except reraise:
            # Control-flow signals (e.g. a user/dashboard stop) must halt the run,
            # not be recorded as a failed application. asyncio.CancelledError is a
            # BaseException and already propagates past the handler below.
            raise
        except Exception as e:  # a crashing executor must still yield a durable result
            res = _result(
                job, config, "failed", started, now(),
                error_code="executor_exception",
                error_message=str(e),
            )
        record(res)
        attempted += 1

    return results
