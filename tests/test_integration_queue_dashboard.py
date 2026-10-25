"""Phase 9 — the queue-run view model that backs the dashboard panel."""

import json

from src.integration.claims import ClaimStore
from src.integration.queue_dashboard import build_queue_view, get_queue_view


def _write_queue(path, jobs):
    doc = {
        "schema": "approved-jobs.v1",
        "generated_at": "2026-10-25T00:00:00Z",
        "min_score": 4.0,
        "executor": "linkedin",
        "jobs": jobs,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def _job(iid, company="Acme", role="Staff Engineer", score=4.2, url=None):
    return {
        "integration_id": iid,
        "canonical_url": url or f"https://www.linkedin.com/jobs/view/{iid.split(':')[-1]}",
        "company": company,
        "role": role,
        "career_ops_score": score,
        "evaluation_confidence": "high",
        "report_number": 1,
        "report_path": "reports/1.md",
        "resume_path": "/abs/cv.pdf",
        "cover_letter_path": None,
        "resume_sha256": "a" * 64,
        "source": "linkedin",
        "application_status": "approved",
        "created_at": "2026-10-25T00:00:00Z",
    }


def _result_line(iid, result, finished_at, **kw):
    rec = {
        "schema": "application-result.v1",
        "executor_run_id": "run-1",
        "integration_id": iid,
        "canonical_url": f"https://www.linkedin.com/jobs/view/{iid.split(':')[-1]}",
        "started_at": finished_at,
        "finished_at": finished_at,
        "result": result,
        "company": "Acme",
        "role": "Staff Engineer",
        "career_ops_score": 4.2,
        "resume_path": "/abs/cv.pdf",
        "cover_letter_path": None,
        "screenshot_paths": [],
        "questions_encountered": [],
        "fields_filled": [],
        "error_code": kw.get("error_code"),
        "error_message": kw.get("error_message"),
        "manual_action_required": kw.get("manual_action_required", False),
    }
    return json.dumps(rec)


def test_view_joins_queue_results_and_claims(tmp_path):
    q = tmp_path / "linkedin-approved.json"
    r = tmp_path / "linkedin-results.jsonl"
    claims = tmp_path / "claims"
    _write_queue(q, [_job("linkedin:1"), _job("linkedin:2", company="Globex"), _job("linkedin:3")])
    r.write_text(
        "\n".join(
            [
                _result_line("linkedin:1", "submitted", "2026-10-25T01:00:00Z"),
                _result_line(
                    "linkedin:2", "manual_review", "2026-10-25T01:05:00Z",
                    error_code="sponsorship", manual_action_required=True,
                ),
            ]
        ),
        encoding="utf-8",
    )
    # linkedin:3 has no result but is claimed -> in_progress.
    ClaimStore(str(claims)).try_claim("linkedin:3", "run-1")

    view = build_queue_view(q, r, claims)
    rows = {row["integration_id"]: row for row in view["jobs"]}

    assert rows["linkedin:1"]["state"] == "applied"
    assert rows["linkedin:2"]["state"] == "needs_review"
    assert rows["linkedin:2"]["reason"] == "sponsorship"
    assert rows["linkedin:3"]["state"] == "in_progress"
    assert rows["linkedin:3"]["claimed"] is True

    s = view["summary"]
    assert s["queued"] == 3
    assert s["with_results"] == 2
    assert s["claimed"] == 1
    assert s["states"] == {"applied": 1, "needs_review": 1, "in_progress": 1}


def test_latest_result_per_job_wins(tmp_path):
    q = tmp_path / "q.json"
    r = tmp_path / "r.jsonl"
    _write_queue(q, [_job("linkedin:1")])
    r.write_text(
        "\n".join(
            [
                _result_line("linkedin:1", "failed", "2026-10-25T01:00:00Z", error_code="x"),
                _result_line("linkedin:1", "submitted", "2026-10-25T02:00:00Z"),
            ]
        ),
        encoding="utf-8",
    )
    view = build_queue_view(q, r, tmp_path / "claims")
    assert view["jobs"][0]["state"] == "applied"  # latest (submitted) wins


def test_result_for_job_not_in_queue_is_marked_prior(tmp_path):
    q = tmp_path / "q.json"
    r = tmp_path / "r.jsonl"
    _write_queue(q, [_job("linkedin:1")])
    r.write_text(
        "\n".join(
            [
                _result_line("linkedin:1", "submitted", "2026-10-25T01:00:00Z"),
                _result_line("linkedin:99", "submitted", "2026-10-25T01:00:00Z"),
            ]
        ),
        encoding="utf-8",
    )
    view = build_queue_view(q, r, tmp_path / "claims")
    prior = [row for row in view["jobs"] if not row["in_queue"]]
    assert len(prior) == 1 and prior[0]["integration_id"] == "linkedin:99"


def test_missing_files_and_torn_line_are_tolerated(tmp_path):
    q = tmp_path / "q.json"
    r = tmp_path / "r.jsonl"
    _write_queue(q, [_job("linkedin:1")])
    r.write_text(_result_line("linkedin:1", "submitted", "2026-10-25T01:00:00Z") + '\n{"torn":', "utf-8")
    view = build_queue_view(q, r, tmp_path / "claims")
    assert view["summary"]["queued"] == 1
    assert view["jobs"][0]["state"] == "applied"

    # Entirely missing files -> empty but well-formed view.
    empty = build_queue_view(tmp_path / "none.json", tmp_path / "none.jsonl", tmp_path / "none")
    assert empty["jobs"] == []
    assert empty["summary"]["queued"] == 0


def test_get_queue_view_unconfigured_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("CAREER_OPS_INTEGRATION_DIR", raising=False)
    view = get_queue_view()
    assert view["configured"] is False
    assert view["jobs"] == []
