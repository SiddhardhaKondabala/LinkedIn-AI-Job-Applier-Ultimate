"""Read-only view model of a queue run, for the dashboard (Phase 9).

Joins the three integration artifacts the executor produces into one picture a
person can monitor:

- the approved-jobs.v1 queue  (what career-ops handed over),
- the application-result.v1 JSONL  (what the executor did with each job), and
- the claims directory  (which jobs a run is holding / has attempted).

It is pure and dependency-light (json + the stdlib): it only reads files, never
writes, and tolerates missing or partially written ones, so the dashboard can
call it any time — before, during, or after a run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from .claims import ClaimStore

#: result value -> coarse state bucket shown in the dashboard.
_RESULT_STATE = {
    "submitted": "applied",
    "already_applied": "applied",
    "manual_review": "needs_review",
    "captcha": "needs_review",
    "requires_mfa": "needs_review",
    "failed": "failed",
    "invalid": "skipped",
    "expired": "skipped",
    "skipped": "skipped",
}

#: Default bridge file names (match bootstrap.sh / .env).
_DEFAULT_QUEUE_NAME = "linkedin-approved.json"
_DEFAULT_RESULTS_NAME = "linkedin-results.jsonl"
_CLAIMS_DIRNAME = "claims"


def resolve_paths() -> Optional[dict]:
    """Resolve the queue/results/claims paths from the integration env.

    Returns None when CAREER_OPS_INTEGRATION_DIR is not set — the dashboard then
    shows an empty queue view rather than guessing a location.
    """
    base = os.environ.get("CAREER_OPS_INTEGRATION_DIR")
    if not base:
        return None
    base_path = Path(base)
    return {
        "queue_path": base_path / os.environ.get("LINKEDIN_APPROVED_QUEUE", _DEFAULT_QUEUE_NAME),
        "results_path": base_path / os.environ.get("LINKEDIN_RESULTS", _DEFAULT_RESULTS_NAME),
        "claims_dir": base_path / _CLAIMS_DIRNAME,
    }


def _load_queue_jobs(queue_path) -> list[dict]:
    p = Path(queue_path)
    if not p.is_file():
        return []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return doc.get("jobs", []) if isinstance(doc, dict) else []


def _load_latest_results(results_path) -> dict[str, dict]:
    """Map integration_id -> its latest result record (by finished_at, then order)."""
    p = Path(results_path)
    if not p.is_file():
        return {}
    latest: dict[str, dict] = {}
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # tolerate a torn trailing line
        iid = rec.get("integration_id")
        if not iid:
            continue
        prev = latest.get(iid)
        if prev is None or str(rec.get("finished_at", "")) >= str(prev.get("finished_at", "")):
            latest[iid] = rec
    return latest


def _row_from_result(rec: dict) -> dict:
    """The dashboard row fields taken from a result record."""
    result = rec.get("result")
    return {
        "result": result,
        "state": _RESULT_STATE.get(result, "other"),
        "reason": rec.get("error_code"),
        "detail": rec.get("error_message"),
        "manual_action_required": bool(rec.get("manual_action_required")),
        "finished_at": rec.get("finished_at"),
        "executor_run_id": rec.get("executor_run_id"),
        "resume_sha256": rec.get("resume_sha256"),
        "cover_letter_sha256": rec.get("cover_letter_sha256"),
    }


def build_queue_view(queue_path, results_path, claims_dir) -> dict:
    """Build the full queue view: {summary, jobs}.

    jobs has one row per queued job (joined with its latest result and claim
    state), plus any result whose job is no longer in the current queue
    (``in_queue`` False), so nothing the executor did is hidden.
    """
    jobs = _load_queue_jobs(queue_path)
    results = _load_latest_results(results_path)
    store = ClaimStore(str(claims_dir))

    rows: list[dict] = []
    seen_ids: set[str] = set()

    for job in jobs:
        iid = job.get("integration_id")
        seen_ids.add(iid)
        claimed = store.is_claimed(iid) if iid else False
        rec = results.get(iid)
        row = {
            "integration_id": iid,
            "company": job.get("company"),
            "role": job.get("role"),
            "career_ops_score": job.get("career_ops_score"),
            "canonical_url": job.get("canonical_url"),
            "resume_sha256": job.get("resume_sha256"),
            "in_queue": True,
            "claimed": claimed,
            "claim_owner": store.owner(iid) if claimed else None,
        }
        if rec is not None:
            row.update(_row_from_result(rec))
        else:
            row["result"] = None
            row["state"] = "in_progress" if claimed else "pending"
        rows.append(row)

    # Results for jobs not in the current queue (e.g. a prior queue's outcomes).
    for iid, rec in results.items():
        if iid in seen_ids:
            continue
        row = {
            "integration_id": iid,
            "company": rec.get("company"),
            "role": rec.get("role"),
            "career_ops_score": rec.get("career_ops_score"),
            "canonical_url": rec.get("canonical_url"),
            "in_queue": False,
            "claimed": store.is_claimed(iid) if iid else False,
            "claim_owner": None,
        }
        row.update(_row_from_result(rec))
        rows.append(row)

    # The summary describes the CURRENT queue, so its state counts add up to
    # `queued`. Rows for results no longer in the queue (in_queue False) still
    # appear in the table but are counted separately as `prior`, not folded into
    # the current-queue states or the claimed count.
    in_queue_rows = [r for r in rows if r.get("in_queue")]
    summary = {
        "queued": len(jobs),
        "with_results": len(results),
        "prior": len(rows) - len(in_queue_rows),
        "claimed": sum(1 for r in in_queue_rows if r.get("claimed")),
        "states": {},
    }
    for r in in_queue_rows:
        state = r.get("state", "pending")
        summary["states"][state] = summary["states"].get(state, 0) + 1

    return {"summary": summary, "jobs": rows}


def get_queue_view() -> dict:
    """Convenience: resolve paths from the env and build the view (empty if unset)."""
    paths = resolve_paths()
    if paths is None:
        return {"summary": {"queued": 0, "with_results": 0, "claimed": 0, "states": {}}, "jobs": [], "configured": False}
    view = build_queue_view(paths["queue_path"], paths["results_path"], paths["claims_dir"])
    view["configured"] = True
    return view
