"""Bot-side half of the versioned bridge contract (Phase 1).

Mirrors career-ops/tests/integration-schema.test.mjs: the bot must reject
unsupported schema versions and missing/invalid fields on both the approved-jobs
queue and the application-result record, so the two repos cannot drift.
"""

import pytest

from src.integration.schemas import (
    APPROVED_JOBS_SCHEMA_ID,
    APPLICATION_RESULT_SCHEMA_ID,
    CONFIDENCE_LEVELS,
    RESULT_VALUES,
    SchemaError,
    assert_application_result,
    assert_approved_jobs,
    validate_application_result,
    validate_approved_jobs,
)


def valid_job(**overrides):
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
    job.update(overrides)
    return job


def valid_queue(jobs=None):
    return {
        "schema": APPROVED_JOBS_SCHEMA_ID,
        "generated_at": "2026-10-24T12:00:00Z",
        "min_score": 4.0,
        "executor": "linkedin",
        "jobs": [valid_job()] if jobs is None else jobs,
    }


def valid_result(**overrides):
    rec = {
        "schema": APPLICATION_RESULT_SCHEMA_ID,
        "executor_run_id": "run-1",
        "integration_id": "linkedin:4012345678",
        "canonical_url": "https://www.linkedin.com/jobs/view/4012345678",
        "started_at": "2026-10-24T12:00:00Z",
        "finished_at": "2026-10-24T12:01:00Z",
        "result": "submitted",
        "confidence": "high",
        "company": "Acme",
        "role": "Staff Engineer",
        "career_ops_score": 4.2,
        "resume_path": "/abs/resume.pdf",
        "cover_letter_path": "/abs/cover.pdf",
        "screenshot_paths": ["/abs/shot-1.png"],
        "questions_encountered": [],
        "fields_filled": ["first_name"],
        "error_code": None,
        "error_message": None,
        "manual_action_required": False,
    }
    rec.update(overrides)
    return rec


def test_valid_queue_passes():
    assert validate_approved_jobs(valid_queue()) == []


def test_unsupported_queue_version_rejected():
    errors = validate_approved_jobs({**valid_queue(), "schema": "approved-jobs.v2"})
    assert errors
    assert any("schema" in e for e in errors)


def test_missing_required_queue_field_rejected():
    q = valid_queue()
    del q["min_score"]
    errors = validate_approved_jobs(q)
    assert any("min_score" in e for e in errors)


def test_missing_required_job_field_rejected():
    job = valid_job()
    del job["resume_path"]
    errors = validate_approved_jobs(valid_queue([job]))
    assert any("resume_path" in e for e in errors)


def test_wrong_typed_score_rejected():
    errors = validate_approved_jobs(valid_queue([valid_job(career_ops_score="4.2")]))
    assert any("career_ops_score" in e for e in errors)


def test_out_of_range_score_rejected():
    assert validate_approved_jobs(valid_queue([valid_job(career_ops_score=6)]))


def test_bad_confidence_enum_rejected():
    errors = validate_approved_jobs(valid_queue([valid_job(evaluation_confidence="certain")]))
    assert any("evaluation_confidence" in e for e in errors)


def test_unexpected_job_property_rejected():
    assert validate_approved_jobs(valid_queue([valid_job(hacked=True)]))


def test_nullable_cover_letter_allowed():
    assert validate_approved_jobs(valid_queue([valid_job(cover_letter_path=None)])) == []


def test_string_report_number_allowed():
    assert validate_approved_jobs(valid_queue([valid_job(report_number="REQ-17")])) == []


def test_assert_approved_jobs_raises():
    with pytest.raises(SchemaError) as exc:
        assert_approved_jobs({**valid_queue(), "schema": "nope"})
    assert exc.value.errors


def test_valid_result_passes():
    assert validate_application_result(valid_result()) == []


def test_every_result_value_valid():
    for value in RESULT_VALUES:
        assert validate_application_result(valid_result(result=value)) == [], value


def test_unknown_result_value_rejected():
    errors = validate_application_result(valid_result(result="definitely_applied"))
    assert any("result" in e for e in errors)


def test_result_confidence_levels_and_null():
    for c in (*CONFIDENCE_LEVELS, None):
        assert validate_application_result(valid_result(confidence=c)) == []
    assert validate_application_result(valid_result(confidence="vibes"))


def test_failed_result_still_valid():
    assert (
        validate_application_result(
            valid_result(result="failed", error_code="E_NAV", error_message="timeout")
        )
        == []
    )


def test_assert_application_result_raises_on_missing_field():
    bad = valid_result()
    del bad["manual_action_required"]
    with pytest.raises(SchemaError):
        assert_application_result(bad)


# --- Contract hardening: timestamps must be ISO-8601 (parity with career-ops) ---


def test_empty_queue_timestamp_rejected():
    errors = validate_approved_jobs({**valid_queue(), "generated_at": ""})
    assert any("generated_at" in e for e in errors)


def test_malformed_queue_timestamp_rejected():
    errors = validate_approved_jobs({**valid_queue(), "generated_at": "2026-10-24 12:00"})
    assert any("generated_at" in e for e in errors)


def test_malformed_job_created_at_rejected():
    errors = validate_approved_jobs(valid_queue([valid_job(created_at="yesterday")]))
    assert any("created_at" in e for e in errors)


def test_malformed_result_timestamp_rejected():
    errors = validate_application_result(valid_result(started_at=""))
    assert any("started_at" in e for e in errors)


def test_timestamp_with_offset_accepted():
    assert validate_approved_jobs({**valid_queue(), "generated_at": "2026-10-24T12:00:00+05:30"}) == []
    assert validate_application_result(valid_result(started_at="2026-10-24T12:00:00.123Z")) == []
