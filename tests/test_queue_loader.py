"""Phase 3 — approved-queue loading/validation.

The queue must validate against approved-jobs.v1 BEFORE any browser work, so an
unsupported version or malformed queue fails fast.
"""

import json

import pytest

from src.integration.queue_loader import QueueError, load_queue, queue_min_score
from src.integration.schemas import APPROVED_JOBS_SCHEMA_ID, SchemaError


def valid_queue(jobs=None):
    job = {
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
    return {
        "schema": APPROVED_JOBS_SCHEMA_ID,
        "generated_at": "2026-10-24T12:00:00Z",
        "min_score": 4.0,
        "executor": "linkedin",
        "jobs": [job] if jobs is None else jobs,
    }


def _write(tmp_path, doc):
    p = tmp_path / "queue.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return str(p)


def test_load_valid_queue(tmp_path):
    doc, jobs = load_queue(_write(tmp_path, valid_queue()))
    assert len(jobs) == 1
    assert jobs[0].integration_id == "linkedin:4012345678"
    assert jobs[0].career_ops_score == 4.2
    assert queue_min_score(doc) == 4.0


def test_unsupported_version_fails_before_browser(tmp_path):
    with pytest.raises(SchemaError):
        load_queue(_write(tmp_path, {**valid_queue(), "schema": "approved-jobs.v2"}))


def test_malformed_queue_rejected(tmp_path):
    bad = valid_queue()
    del bad["min_score"]
    with pytest.raises(SchemaError):
        load_queue(_write(tmp_path, bad))


def test_missing_file_raises_queue_error(tmp_path):
    with pytest.raises(QueueError):
        load_queue(str(tmp_path / "does-not-exist.json"))


def test_bad_json_raises_queue_error(tmp_path):
    p = tmp_path / "queue.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(QueueError):
        load_queue(str(p))
