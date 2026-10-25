"""Phase 3 — queue-mode execution (M2).

The runner processes a mocked career-ops queue WITHOUT any search or scoring,
delegating the real application to an injectable apply_fn. These tests pin the
gating, safety, ordering, and result mapping with a mock apply_fn — no browser.
"""

import pytest

from src.integration.executor_config import QueueExecutionConfig
from src.integration.queue_models import ApplicationResult, ApprovedJob
from src.integration.queue_runner import is_valid_linkedin_url, process_queue
from src.integration.schemas import assert_application_result

pytestmark = pytest.mark.asyncio

NOW = "2026-10-24T12:00:00Z"


def make_job(**kw):
    d = {
        "integration_id": "linkedin:4012345678",
        "canonical_url": "https://www.linkedin.com/jobs/view/4012345678",
        "company": "Acme",
        "role": "Staff Engineer",
        "career_ops_score": 4.2,
        "evaluation_confidence": "high",
        "report_number": 17,
        "report_path": "reports/17.md",
        "resume_path": "/abs/resume.pdf",
        "cover_letter_path": "/abs/cover.pdf",
        "source": "linkedin",
        "application_status": "approved",
        "created_at": "2026-10-24T12:00:00Z",
    }
    d.update(kw)
    return ApprovedJob.from_dict(d)


def cfg(**kw):
    kw.setdefault("executor_run_id", "run-1")
    kw.setdefault("min_score", 4.0)
    return QueueExecutionConfig(**kw)


def recorder(result="submitted"):
    calls = []

    async def apply_fn(job, config):
        calls.append((job, config))
        return ApplicationResult.for_job(
            job, executor_run_id=config.executor_run_id, result=result, started_at=NOW, finished_at=NOW
        )

    return apply_fn, calls


ALWAYS = lambda _p: True  # noqa: E731


async def test_sequential_processing_calls_apply_for_each():
    jobs = [
        make_job(canonical_url="https://www.linkedin.com/jobs/view/111"),
        make_job(canonical_url="https://www.linkedin.com/jobs/view/222"),
        make_job(canonical_url="https://www.linkedin.com/jobs/view/333"),
    ]
    apply_fn, calls = recorder()
    results = await process_queue(jobs, apply_fn, cfg(), resume_exists=ALWAYS)
    assert [r.canonical_url for r in results] == [j.canonical_url for j in jobs]
    assert len(calls) == 3
    assert all(r.result == "submitted" for r in results)


async def test_below_min_score_skipped_without_applying():
    apply_fn, calls = recorder()
    results = await process_queue([make_job(career_ops_score=3.9)], apply_fn, cfg(), resume_exists=ALWAYS)
    assert results[0].result == "skipped"
    assert results[0].error_code == "below_min_score"
    assert calls == []


async def test_invalid_linkedin_url_is_invalid():
    apply_fn, calls = recorder()
    results = await process_queue(
        [make_job(canonical_url="https://boards.greenhouse.io/acme/jobs/1")], apply_fn, cfg(), resume_exists=ALWAYS
    )
    assert results[0].result == "invalid"
    assert results[0].error_code == "invalid_linkedin_url"
    assert results[0].manual_action_required is True
    assert calls == []


async def test_missing_resume_fails_safely():
    apply_fn, calls = recorder()
    results = await process_queue([make_job()], apply_fn, cfg(), resume_exists=lambda _p: False)
    assert results[0].result == "invalid"
    assert results[0].error_code == "missing_resume"
    assert calls == []


async def test_dry_run_never_calls_apply():
    apply_fn, calls = recorder()
    results = await process_queue([make_job()], apply_fn, cfg(dry_run=True), resume_exists=ALWAYS)
    assert results[0].result == "skipped"
    assert results[0].error_code == "dry_run"
    assert calls == []


async def test_test_mode_never_submits():
    apply_fn, calls = recorder()
    await process_queue([make_job()], apply_fn, cfg(submit=True, test_mode=True), resume_exists=ALWAYS)
    # apply_fn received a config whose should_submit is False in test mode.
    assert calls[0][1].should_submit is False


async def test_default_config_does_not_submit_but_submit_flag_does():
    assert cfg().should_submit is False
    assert cfg(submit=True).should_submit is True
    assert cfg(submit=True, dry_run=True).should_submit is False


@pytest.mark.parametrize("result", ["submitted", "already_applied", "captcha", "requires_mfa", "failed"])
async def test_result_mapping_passthrough(result):
    apply_fn, _ = recorder(result=result)
    results = await process_queue([make_job()], apply_fn, cfg(), resume_exists=ALWAYS)
    assert results[0].result == result


async def test_max_jobs_limits_attempts():
    jobs = [
        make_job(canonical_url="https://www.linkedin.com/jobs/view/111"),
        make_job(canonical_url="https://www.linkedin.com/jobs/view/222"),
    ]
    apply_fn, calls = recorder()
    results = await process_queue(jobs, apply_fn, cfg(max_jobs=1), resume_exists=ALWAYS)
    assert len(calls) == 1
    assert len(results) == 1


async def test_reraise_exception_halts_the_run():
    class Stop(Exception):
        pass

    async def boom(job, config):
        raise Stop("stop requested")

    # A control-flow signal in `reraise` propagates instead of becoming a result.
    with pytest.raises(Stop):
        await process_queue([make_job()], boom, cfg(), resume_exists=ALWAYS, reraise=(Stop,))


async def test_crashing_executor_still_yields_a_failed_result():
    async def boom(job, config):
        raise RuntimeError("browser exploded")

    results = await process_queue([make_job()], boom, cfg(), resume_exists=ALWAYS)
    assert results[0].result == "failed"
    assert results[0].error_code == "executor_exception"
    assert "browser exploded" in results[0].error_message


async def test_pause_checker_is_awaited_per_job():
    seen = {"n": 0}

    async def pause():
        seen["n"] += 1

    apply_fn, _ = recorder()
    await process_queue([make_job(), make_job(canonical_url="https://www.linkedin.com/jobs/view/222")],
                        apply_fn, cfg(), pause_checker=pause, resume_exists=ALWAYS)
    assert seen["n"] == 2


async def test_results_validate_against_schema():
    apply_fn, _ = recorder()
    results = await process_queue([make_job()], apply_fn, cfg(), resume_exists=ALWAYS)
    assert_application_result(results[0].to_dict())  # raises if invalid


async def test_is_valid_linkedin_url():
    assert is_valid_linkedin_url("https://www.linkedin.com/jobs/view/4012345678")
    assert is_valid_linkedin_url("https://www.linkedin.com/comm/jobs/view/123")
    assert not is_valid_linkedin_url("https://boards.greenhouse.io/x/jobs/1")
    assert not is_valid_linkedin_url("https://linkedin.com.evil.example/jobs/view/1")
    assert not is_valid_linkedin_url(None)
