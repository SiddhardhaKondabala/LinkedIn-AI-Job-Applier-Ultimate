"""Phase 3 — LinkedInJobManager.apply_approved_job (queue mode, no search).

Mocks the browser-touching methods so we can pin the orchestration: it navigates
to the approved URL, skips the search / is-interesting gate, honors should_submit
via Easy Apply's test_mode, and returns a raw outcome dict.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.integration.queue_models import ApprovedJob
from src.job_manager.linkedin.job_manager_linkedin import LinkedInJobManager

pytestmark = pytest.mark.asyncio

MOD = "src.job_manager.linkedin.job_manager_linkedin"


def make_jm():
    page = MagicMock()
    new_page = MagicMock()
    new_page.goto = AsyncMock()
    new_page.close = AsyncMock()
    page.context.new_page = AsyncMock(return_value=new_page)
    jm = LinkedInJobManager(page, "me@example.test", MagicMock(), MagicMock())
    jm.llm_answerer_component = MagicMock()
    jm.llm_agent_component = MagicMock()
    jm.llm_agent_component.apply_to_job = AsyncMock(return_value=("Success", ""))
    return jm, new_page


def job_mock(valid=True):
    j = MagicMock()
    j.is_valid_for_application.return_value = valid
    j.model_dump.return_value = {}
    return j


def approved():
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
            "cover_letter_path": None,
            "source": "linkedin",
            "application_status": "approved",
            "created_at": "2026-10-24T12:00:00Z",
        }
    )


async def test_applies_without_search_and_test_mode_when_not_submitting():
    jm, new_page = make_jm()
    easy = AsyncMock(return_value=("Success", ""))
    with patch(f"{MOD}.async_pause", new=AsyncMock()), patch.object(
        jm, "_get_detailed_job_description", new=AsyncMock(return_value=job_mock())
    ), patch.object(jm, "_check_apply_button", new=AsyncMock(return_value=None)), patch.object(
        jm, "easy_apply", new=easy
    ):
        out = await jm.apply_approved_job(approved(), should_submit=False)

    assert out["status"] == "Success"
    assert out["already_applied"] is False
    new_page.goto.assert_awaited()
    assert easy.await_args.kwargs["test_mode"] is True  # not should_submit
    # Queue mode never searches or re-scores.
    jm.search_component.set_search_params.assert_not_called()
    jm.llm_answerer_component.job_is_interesting.assert_not_called()


async def test_submit_true_runs_easy_apply_without_test_mode():
    jm, _ = make_jm()
    easy = AsyncMock(return_value=("Success", ""))
    with patch(f"{MOD}.async_pause", new=AsyncMock()), patch.object(
        jm, "_get_detailed_job_description", new=AsyncMock(return_value=job_mock())
    ), patch.object(jm, "_check_apply_button", new=AsyncMock(return_value=None)), patch.object(
        jm, "easy_apply", new=easy
    ):
        await jm.apply_approved_job(approved(), should_submit=True)
    assert easy.await_args.kwargs["test_mode"] is False


async def test_invalid_posting_returns_skip():
    jm, _ = make_jm()
    with patch(f"{MOD}.async_pause", new=AsyncMock()), patch.object(
        jm, "_get_detailed_job_description", new=AsyncMock(return_value=job_mock(valid=False))
    ):
        out = await jm.apply_approved_job(approved(), should_submit=True)
    assert out["status"] == "Skip"
    assert "not valid" in out["reason"].lower()


async def test_external_apply_requires_manual_review_when_not_allowed():
    jm, _ = make_jm()
    easy = AsyncMock(return_value=("Success", ""))
    with patch(f"{MOD}.async_pause", new=AsyncMock()), patch(f"{MOD}.EASY_APPLY_ONLY_MODE", False), patch.object(
        jm, "_get_detailed_job_description", new=AsyncMock(return_value=job_mock())
    ), patch.object(
        jm, "_check_apply_button", new=AsyncMock(return_value="https://external.example/apply")
    ), patch.object(jm, "easy_apply", new=easy):
        out = await jm.apply_approved_job(approved(), should_submit=True, allow_external=False)

    assert out["status"] == "Skip"
    assert "external apply requires manual review" in out["reason"].lower()
    easy.assert_not_called()
    jm.llm_agent_component.apply_to_job.assert_not_called()


async def test_skip_listed_external_ats_is_not_sent_to_easy_apply():
    jm, _ = make_jm()
    easy = AsyncMock(return_value=("Success", ""))
    with patch(f"{MOD}.async_pause", new=AsyncMock()), patch(f"{MOD}.EASY_APPLY_ONLY_MODE", False), patch(
        f"{MOD}.EXTERNAL_APPLY_SKIP_ATSES", ("skipme.example",)
    ), patch.object(jm, "_get_detailed_job_description", new=AsyncMock(return_value=job_mock())), patch.object(
        jm, "_check_apply_button", new=AsyncMock(return_value="https://skipme.example/apply")
    ), patch.object(jm, "easy_apply", new=easy):
        out = await jm.apply_approved_job(approved(), should_submit=True, allow_external=True)

    assert out["status"] == "Skip"
    assert "unsupported external ats" in out["reason"].lower()
    easy.assert_not_called()
    jm.llm_agent_component.apply_to_job.assert_not_called()
