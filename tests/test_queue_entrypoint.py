"""Queue entrypoint safety: dry-run must not initialize bot/browser dependencies."""

from __future__ import annotations

import json
from hashlib import sha256

import pytest

import main
from src.integration.cli import parse_queue_args


@pytest.mark.asyncio
async def test_dry_run_never_initializes_executor(monkeypatch, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 approved")
    queue = tmp_path / "queue.json"
    queue.write_text(
        json.dumps(
            {
                "schema": "approved-jobs.v1",
                "generated_at": "2026-10-25T12:00:00Z",
                "min_score": 4.0,
                "executor": "linkedin",
                "jobs": [
                    {
                        "integration_id": "linkedin:4012345678",
                        "canonical_url": "https://www.linkedin.com/jobs/view/4012345678",
                        "company": "Acme",
                        "role": "Staff Engineer",
                        "career_ops_score": 4.2,
                        "evaluation_confidence": "high",
                        "report_number": 1,
                        "report_path": str(tmp_path / "report.md"),
                        "resume_path": str(resume),
                        "resume_sha256": sha256(resume.read_bytes()).hexdigest(),
                        "cover_letter_path": None,
                        "source": "linkedin",
                        "application_status": "approved",
                        "created_at": "2026-10-25T00:00:00Z",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    results = tmp_path / "results.jsonl"

    class MustNotInitialize:
        def __init__(self):
            raise AssertionError("dry-run initialized ConfigValidator/browser dependencies")

    async def must_not_run(*_args, **_kwargs):
        raise AssertionError("dry-run initialized the bot")

    monkeypatch.setattr(main, "ConfigValidator", MustNotInitialize)
    monkeypatch.setattr(main, "create_and_run_bot", must_not_run)
    args = parse_queue_args(["--approved-jobs", str(queue), "--results", str(results), "--dry-run"])

    assert await main.run_queue_mode(args) is True
    record = json.loads(results.read_text(encoding="utf-8").strip())
    assert record["result"] == "skipped"
    assert record["error_code"] == "dry_run"
