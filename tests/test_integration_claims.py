"""Phase 8 — job claiming so the same job is never applied to twice."""

from src.integration.claims import (
    KEEP_CLAIM_RESULTS,
    ClaimStore,
    claimant_from_store,
)
from src.integration.queue_models import ApprovedJob


def make_job(integration_id="linkedin:111"):
    return ApprovedJob.from_dict(
        {
            "integration_id": integration_id,
            "canonical_url": "https://www.linkedin.com/jobs/view/111",
            "company": "Acme",
            "role": "Staff Engineer",
            "career_ops_score": 4.2,
            "evaluation_confidence": "high",
            "report_number": 1,
            "report_path": "reports/1.md",
            "resume_path": "/abs/resume.pdf",
            "cover_letter_path": None,
            "source": "linkedin",
            "application_status": "approved",
            "created_at": "2026-10-24T12:00:00Z",
        }
    )


def test_first_claim_wins_second_is_refused(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    assert store.try_claim("linkedin:111", "run-a") is True
    assert store.try_claim("linkedin:111", "run-b") is False  # another run holds it
    assert store.owner("linkedin:111") == "run-a"


def test_release_reopens_the_job(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    assert store.try_claim("linkedin:111", "run-a") is True
    store.release("linkedin:111")
    assert store.owner("linkedin:111") is None
    assert store.try_claim("linkedin:111", "run-b") is True  # retryable after release


def test_release_missing_claim_is_safe(tmp_path):
    ClaimStore(str(tmp_path / "claims")).release("never-claimed")  # no error


def test_distinct_jobs_do_not_collide(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    assert store.try_claim("linkedin:1", "r") is True
    assert store.try_claim("linkedin:2", "r") is True


def test_claimant_helpers_bind_to_the_store(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    claim, release = claimant_from_store(store)
    job = make_job("linkedin:999")
    assert claim(job, "run-a") is True
    assert claim(job, "run-b") is False
    release(job)
    assert claim(job, "run-b") is True


def test_keep_claim_results_are_terminal_only():
    assert "submitted" in KEEP_CLAIM_RESULTS
    assert "manual_review" in KEEP_CLAIM_RESULTS
    assert "failed" not in KEEP_CLAIM_RESULTS  # transient -> released for retry
