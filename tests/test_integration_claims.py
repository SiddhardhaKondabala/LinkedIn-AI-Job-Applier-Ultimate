"""Phase 8 — job claiming so the same job is never applied to twice."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from src.integration.claims import (
    KEEP_CLAIM_RESULTS,
    ClaimStateError,
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


def test_lock_file_is_stable_after_claim_release(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    assert store.try_claim("linkedin:111", "run-a") is True
    lock_path = store._lock_path("linkedin:111")
    assert lock_path.exists()
    store.release("linkedin:111")
    assert lock_path.exists()  # deleting a live coordination inode is unsafe


def test_expired_claim_is_reclaimed(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"), timeout_seconds=1)
    assert store.try_claim("linkedin:111", "run-a") is True
    path = store._path("linkedin:111")
    os.utime(path, (0, 0))
    assert store.try_claim("linkedin:111", "run-b") is True
    assert store.owner("linkedin:111") == "run-b"


def test_only_one_concurrent_reclaimer_wins(tmp_path):
    class SlowExpiredStore(ClaimStore):
        def _is_expired(self, path):
            if time.time() - path.stat().st_mtime > 1:
                time.sleep(0.03)
            return super()._is_expired(path)

    store = SlowExpiredStore(str(tmp_path / "claims"), timeout_seconds=1)
    assert store.try_claim("linkedin:111", "old") is True
    os.utime(store._path("linkedin:111"), (0, 0))
    barrier = Barrier(8)

    def reclaim(index):
        barrier.wait()
        return store.try_claim("linkedin:111", f"run-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(reclaim, range(8)))

    assert outcomes.count(True) == 1


def test_zero_timeout_disables_expiration(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"), timeout_seconds=0)
    assert store.try_claim("linkedin:111", "run-a") is True
    path = store._path("linkedin:111")
    os.utime(path, (0, 0))
    assert store.try_claim("linkedin:111", "run-b") is False


def test_distinct_jobs_do_not_collide(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    assert store.try_claim("linkedin:1", "r") is True
    assert store.try_claim("linkedin:2", "r") is True


def test_claimant_helpers_bind_to_the_store(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    claim, release, finalize = claimant_from_store(store)
    job = make_job("linkedin:999")
    assert claim(job, "run-a") is True
    assert claim(job, "run-b") is False
    release(job)
    assert claim(job, "run-b") is True
    finalize(job, "submitted")
    assert store.owner(job.integration_id) == "run-b"


def test_terminal_claim_never_expires(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"), timeout_seconds=1)
    assert store.try_claim("linkedin:111", "run-a") is True
    store.finalize("linkedin:111", "submitted")
    path = store._path("linkedin:111")
    os.utime(path, (0, 0))
    assert store.try_claim("linkedin:111", "run-b") is False
    assert store.owner("linkedin:111") == "run-a"


@pytest.mark.parametrize("contents", [None, "not json", "[]"])
def test_finalize_refuses_missing_or_malformed_claim(tmp_path, contents):
    store = ClaimStore(str(tmp_path / "claims"))
    store.dir.mkdir(parents=True)
    if contents is not None:
        store._path("linkedin:111").write_text(contents, encoding="utf-8")
    with pytest.raises(ClaimStateError, match="cannot finalize"):
        store.finalize("linkedin:111", "submitted")


def test_keep_claim_results_are_terminal_only():
    assert "submitted" in KEEP_CLAIM_RESULTS
    assert "manual_review" in KEEP_CLAIM_RESULTS
    assert "failed" not in KEEP_CLAIM_RESULTS  # transient -> released for retry
