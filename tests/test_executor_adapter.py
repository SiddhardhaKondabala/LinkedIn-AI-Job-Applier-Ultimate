"""Phase 3 — the adapter that turns the job manager into the runner's apply_fn."""

from unittest.mock import AsyncMock

import pytest

from src.integration.executor_adapter import make_apply_fn
from src.integration.executor_config import QueueExecutionConfig
from src.integration.queue_models import ApprovedJob
from src.integration.schemas import assert_application_result

pytestmark = pytest.mark.asyncio

NOW = "2026-10-24T12:00:00Z"


def make_job():
    return ApprovedJob.from_dict(
        {
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
    )


def jm_returning(outcome):
    jm = AsyncMock()
    jm.apply_approved_job = AsyncMock(return_value=outcome)
    return jm


async def test_maps_success_and_passes_should_submit():
    jm = jm_returning({"status": "Success", "reason": "", "already_applied": False})
    fn = make_apply_fn(jm, now=lambda: NOW)
    res = await fn(make_job(), QueueExecutionConfig(executor_run_id="run-1", submit=True))
    assert res.result == "submitted"
    assert res.executor_run_id == "run-1"
    assert res.started_at == NOW and res.finished_at == NOW
    assert_application_result(res.to_dict())
    assert jm.apply_approved_job.await_args.kwargs["should_submit"] is True


async def test_maps_already_applied():
    jm = jm_returning({"status": "Skip", "reason": "Already applied to this job", "already_applied": True})
    res = await make_apply_fn(jm, now=lambda: NOW)(make_job(), QueueExecutionConfig(executor_run_id="r"))
    assert res.result == "already_applied"
    assert res.error_code is None


async def test_maps_error_to_failed_with_message():
    jm = jm_returning({"status": "Error", "reason": "navigation blew up", "already_applied": False})
    res = await make_apply_fn(jm, now=lambda: NOW)(make_job(), QueueExecutionConfig(executor_run_id="r"))
    assert res.result == "failed"
    assert res.error_code == "apply_error"
    assert res.error_message == "navigation blew up"


async def test_default_does_not_submit():
    jm = jm_returning({"status": "Skip", "reason": "Test mode", "already_applied": False})
    await make_apply_fn(jm, now=lambda: NOW)(make_job(), QueueExecutionConfig(executor_run_id="r"))
    assert jm.apply_approved_job.await_args.kwargs["should_submit"] is False
