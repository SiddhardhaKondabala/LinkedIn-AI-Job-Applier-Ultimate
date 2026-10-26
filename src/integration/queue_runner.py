"""Queue-mode executor.

Processes a career-ops approved queue sequentially WITHOUT any independent
search or scoring: it never calls SearchCustomizer, never paginates, and never
uses the LLM "is this interesting?" gate. The actual application is delegated to
an injectable ``apply_fn`` (the real one wraps the LinkedIn job manager; tests
pass a mock), so this orchestration is fully unit-testable without a browser.

Per job, in order: enforce the queue's min score, require a valid LinkedIn URL,
require the tailored resume to exist, verify artifact integrity, claim the job
so no other run double-applies it (Phase 8), honor pause/stop between jobs, and
record exactly one ApplicationResult — including for skips and failures.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .artifacts import ArtifactError
from .claims import KEEP_CLAIM_RESULTS
from .executor_config import QueueExecutionConfig
from .queue_models import ApplicationResult, ApprovedJob, utc_now_iso

ApplyFn = Callable[[ApprovedJob, QueueExecutionConfig], Awaitable[ApplicationResult]]
ClaimFn = Callable[[ApprovedJob, str], bool]
ReleaseFn = Callable[[ApprovedJob], None]
FinalizeFn = Callable[[ApprovedJob, str], None]


class ClaimFinalizationError(RuntimeError):
    """A terminal result was recorded but its claim could not be finalized."""


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
    verify_artifacts: Optional[Callable[[ApprovedJob], object]] = None,
    claim: Optional[ClaimFn] = None,
    release_claim: Optional[ReleaseFn] = None,
    finalize_claim: Optional[FinalizeFn] = None,
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
                    job,
                    config,
                    "skipped",
                    now(),
                    now(),
                    error_code="below_min_score",
                    error_message=f"score {job.career_ops_score} < min {config.min_score}",
                )
            )
            continue

        if not is_valid_linkedin_url(job.canonical_url):
            record(
                _result(
                    job,
                    config,
                    "invalid",
                    now(),
                    now(),
                    error_code="invalid_linkedin_url",
                    error_message=job.canonical_url,
                    manual_action_required=True,
                )
            )
            continue

        if not resume_exists(job.resume_path):
            record(
                _result(
                    job,
                    config,
                    "invalid",
                    now(),
                    now(),
                    error_code="missing_resume",
                    error_message=job.resume_path,
                    manual_action_required=True,
                )
            )
            continue

        # Integrity gate (Phase 6): confirm the queued resume/cover letter still
        # match the exact bytes career-ops approved before we drive the browser.
        # Opt-in so unit tests with synthetic paths stay browser- and disk-free;
        # main wires the real resolver and also preflights the whole queue.
        if verify_artifacts is not None:
            try:
                verify_artifacts(job)
            except ArtifactError as e:
                record(
                    _result(
                        job,
                        config,
                        e.result,
                        now(),
                        now(),
                        error_code=e.error_code,
                        error_message=str(e),
                        manual_action_required=e.manual_action_required,
                    )
                )
                continue

        if config.dry_run:
            record(
                _result(
                    job,
                    config,
                    "skipped",
                    now(),
                    now(),
                    error_code="dry_run",
                    error_message="dry-run: queue validated, not executed",
                )
            )
            continue

        # Claim the job before applying (Phase 8). If another run holds the
        # claim — or a previous run already attempted it — skip rather than
        # risk a second application.
        if claim is not None and not claim(job, config.executor_run_id):
            record(
                _result(
                    job,
                    config,
                    "skipped",
                    now(),
                    now(),
                    error_code="already_claimed",
                    error_message=f"already claimed for {job.integration_id}",
                )
            )
            continue

        started = now()
        try:
            res = await apply_fn(job, config)
        except reraise:
            # Control-flow signals (e.g. a user/dashboard stop) must halt the run,
            # not be recorded as a failed application. The job was never applied,
            # so release its claim to leave it open for a resume. CancelledError
            # is a BaseException and already propagates past the handler below.
            if release_claim is not None:
                release_claim(job)
            raise
        except Exception as e:  # a crashing executor must still yield a durable result
            res = _result(
                job,
                config,
                "failed",
                started,
                now(),
                error_code="executor_exception",
                error_message=str(e),
            )
        # Make a terminal claim durable BEFORE publishing the result. If the
        # process dies between these two writes, preventing a double submission
        # is more important than dashboard completeness; the inverse order left
        # a small window where a submitted result existed but its active claim
        # could later expire and reopen the job.
        if res.result in KEEP_CLAIM_RESULTS:
            if finalize_claim is not None:
                try:
                    finalize_claim(job, res.result)
                except Exception as exc:
                    # Preserve the confirmed outcome in the durable result
                    # journal, but stop the run loudly. Continuing would hide a
                    # claim that is still active and may become reclaimable at
                    # its TTL; an operator must reconcile it before retrying.
                    record(res)
                    attempted += 1
                    raise ClaimFinalizationError(
                        f"result recorded but terminal claim finalization failed "
                        f"for {job.integration_id}; do not retry automatically"
                    ) from exc
        record(res)
        attempted += 1
        # Transient outcomes are retryable only after their durable result has
        # been published; terminal/human-stop claims remain forever.
        if res.result not in KEEP_CLAIM_RESULTS and release_claim is not None:
            release_claim(job)

    return results
