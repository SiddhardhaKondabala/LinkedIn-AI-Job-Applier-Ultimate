"""Phase 6 — shared artifact handoff (M5).

The executor must upload the EXACT career-ops resume/cover letter named in the
approved queue, verified against its SHA-256. These tests cover the browser-free
artifact module, the result echo, and the Easy Apply pinned-upload path.
"""

import hashlib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.integration.artifacts import (
    ArtifactError,
    preflight_artifacts,
    resolve_job_artifacts,
    sha256_file,
)
from src.integration.queue_models import ApplicationResult, ApprovedJob


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
        "cover_letter_path": None,
        "source": "linkedin",
        "application_status": "approved",
        "created_at": "2026-10-24T12:00:00Z",
    }
    d.update(kw)
    return ApprovedJob.from_dict(d)


def write_pdf(path: Path, body: bytes = b"%PDF-1.4 resume") -> str:
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


# --- sha256_file -----------------------------------------------------------


def test_sha256_file_matches_hashlib(tmp_path):
    f = tmp_path / "cv.pdf"
    expected = write_pdf(f)
    assert sha256_file(str(f)) == expected


# --- resolve_job_artifacts -------------------------------------------------


def test_resolve_returns_absolute_paths(tmp_path):
    resume = tmp_path / "cv.pdf"
    cover = tmp_path / "cover.pdf"
    r_sha = write_pdf(resume)
    c_sha = write_pdf(cover, b"%PDF-1.4 cover")
    job = make_job(
        resume_path=str(resume),
        resume_sha256=r_sha,
        cover_letter_path=str(cover),
        cover_letter_sha256=c_sha,
    )
    resolved = resolve_job_artifacts(job)
    assert resolved.resume_path == str(resume.resolve())
    assert resolved.cover_letter_path == str(cover.resolve())


def test_resolve_rejects_missing_hash(tmp_path):
    resume = tmp_path / "cv.pdf"
    write_pdf(resume)
    with pytest.raises(ArtifactError) as ei:
        resolve_job_artifacts(make_job(resume_path=str(resume)))
    assert ei.value.error_code == "missing_resume_hash"
    assert ei.value.result == "manual_review"


def test_resolve_raises_on_missing_resume(tmp_path):
    job = make_job(resume_path=str(tmp_path / "nope.pdf"))
    with pytest.raises(ArtifactError) as ei:
        resolve_job_artifacts(job)
    assert ei.value.error_code == "missing_resume"
    assert ei.value.result == "invalid"


def test_resolve_raises_on_resume_hash_mismatch(tmp_path):
    resume = tmp_path / "cv.pdf"
    write_pdf(resume)
    job = make_job(resume_path=str(resume), resume_sha256="0" * 64)
    with pytest.raises(ArtifactError) as ei:
        resolve_job_artifacts(job)
    assert ei.value.error_code == "resume_hash_mismatch"
    assert ei.value.result == "manual_review"
    assert ei.value.manual_action_required is True


def test_resolve_raises_on_missing_cover_letter(tmp_path):
    resume = tmp_path / "cv.pdf"
    resume_sha = write_pdf(resume)
    job = make_job(
        resume_path=str(resume),
        resume_sha256=resume_sha,
        cover_letter_path=str(tmp_path / "cover-gone.pdf"),
    )
    with pytest.raises(ArtifactError) as ei:
        resolve_job_artifacts(job)
    assert ei.value.error_code == "missing_cover_letter"


# --- preflight_artifacts ---------------------------------------------------


def test_preflight_reports_only_the_problem_jobs(tmp_path):
    good_resume = tmp_path / "good.pdf"
    good_sha = write_pdf(good_resume)
    good = make_job(integration_id="good", resume_path=str(good_resume), resume_sha256=good_sha)
    bad = make_job(integration_id="bad", resume_path=str(tmp_path / "missing.pdf"))
    problems = preflight_artifacts([good, bad])
    assert [job.integration_id for job, _ in problems] == ["bad"]
    assert problems[0][1].error_code == "missing_resume"


# --- result echoes the hashes ----------------------------------------------


def test_result_echoes_queue_hashes():
    job = make_job(resume_sha256="a" * 64, cover_letter_sha256="b" * 64)
    result = ApplicationResult.for_job(
        job, executor_run_id="run-1", result="submitted", started_at="x", finished_at="y"
    )
    doc = result.to_dict()
    assert doc["resume_sha256"] == "a" * 64
    assert doc["cover_letter_sha256"] == "b" * 64


# --- Easy Apply uploads the pinned resume ----------------------------------

MOD = "src.job_manager.linkedin.easy_applier_linkedin"
BASE_MOD = "src.job_manager.easy_applier"


def build_applier(resume_dir: Path):
    from src.job_manager.linkedin.easy_applier_linkedin import LinkedInEasyApplier

    page = MagicMock()
    with (
        patch(f"{MOD}.get_ready_made_resume", return_value=None),
        patch(f"{MOD}.get_ready_made_photo", return_value=None),
        patch.object(LinkedInEasyApplier, "_load_questions", return_value=[]),
    ):
        return LinkedInEasyApplier(
            page=page,
            gpt_answerer=MagicMock(),
            resume_anonymizer=MagicMock(),
            resume_generator_manager=MagicMock(selected_style=None),
            pause_checker=None,
            answers_file=Path("answers.yaml"),
            resume_dir=resume_dir,
            cover_letter_dir=resume_dir,
            test_mode=True,
        )


@pytest.mark.asyncio
async def test_pinned_resume_is_uploaded_verbatim(tmp_path):
    resume = tmp_path / "cv-017.pdf"
    write_pdf(resume)
    applier = build_applier(tmp_path)
    applier.set_pinned_artifacts(resume_path=str(resume))

    element = MagicMock()
    element.set_input_files = AsyncMock()
    job = MagicMock(company_name="Acme", job_title="Staff Engineer")

    with patch(f"{BASE_MOD}.async_pause", new=AsyncMock()):
        await applier._create_and_upload_resume(element, job)

    # The exact approved file was uploaded — no generation was attempted.
    element.set_input_files.assert_awaited_once_with(str(resume.resolve()))
    applier.resume_generator_manager.pdf_base64.assert_not_called()
    assert applier.submitted_resume_path == str(resume.resolve())
