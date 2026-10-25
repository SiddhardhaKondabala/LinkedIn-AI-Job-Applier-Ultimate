"""Typed models for the approved-job queue and application results.

ApprovedJob mirrors one item of approved-jobs.v1; ApplicationResult mirrors
application-result.v1. Both are plain dataclasses — schema validation lives in
schemas.py, so these stay dependency-light and easy to construct in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .schemas import APPLICATION_RESULT_SCHEMA_ID


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 instant (matches the schema pattern)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ApprovedJob:
    """One career-ops-approved job to apply to."""

    integration_id: str
    canonical_url: str
    company: str
    role: str
    career_ops_score: float
    evaluation_confidence: str
    report_number: Any
    report_path: str
    resume_path: str
    cover_letter_path: Optional[str]
    source: str
    application_status: str
    created_at: str
    requisition_id: Optional[str] = None
    #: Hex SHA-256 of the exact artifact career-ops approved, or None. The
    #: executor recomputes and compares before upload (Phase 6).
    resume_sha256: Optional[str] = None
    cover_letter_sha256: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "ApprovedJob":
        return cls(
            integration_id=d["integration_id"],
            canonical_url=d["canonical_url"],
            company=d["company"],
            role=d["role"],
            career_ops_score=d["career_ops_score"],
            evaluation_confidence=d["evaluation_confidence"],
            report_number=d["report_number"],
            report_path=d["report_path"],
            resume_path=d["resume_path"],
            cover_letter_path=d.get("cover_letter_path"),
            source=d["source"],
            application_status=d["application_status"],
            created_at=d["created_at"],
            requisition_id=d.get("requisition_id"),
            resume_sha256=d.get("resume_sha256"),
            cover_letter_sha256=d.get("cover_letter_sha256"),
        )


@dataclass
class ApplicationResult:
    """One durable result record per job attempt (application-result.v1)."""

    executor_run_id: str
    integration_id: str
    canonical_url: str
    started_at: str
    finished_at: str
    result: str
    company: str
    role: str
    career_ops_score: float
    resume_path: str
    cover_letter_path: Optional[str]
    confidence: Optional[str] = None
    screenshot_paths: list = field(default_factory=list)
    questions_encountered: list = field(default_factory=list)
    fields_filled: list = field(default_factory=list)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    manual_action_required: bool = False
    #: The artifact hashes echoed back from the queue, so the imported result
    #: records exactly which resume/cover letter the executor used (Phase 6).
    resume_sha256: Optional[str] = None
    cover_letter_sha256: Optional[str] = None

    @classmethod
    def for_job(
        cls,
        job: ApprovedJob,
        *,
        executor_run_id: str,
        result: str,
        started_at: str,
        finished_at: str,
        **kw: Any,
    ) -> "ApplicationResult":
        """Build a result carrying the job's identity and artifacts."""
        return cls(
            executor_run_id=executor_run_id,
            integration_id=job.integration_id,
            canonical_url=job.canonical_url,
            started_at=started_at,
            finished_at=finished_at,
            result=result,
            company=job.company,
            role=job.role,
            career_ops_score=job.career_ops_score,
            resume_path=job.resume_path,
            cover_letter_path=job.cover_letter_path,
            resume_sha256=kw.pop("resume_sha256", job.resume_sha256),
            cover_letter_sha256=kw.pop("cover_letter_sha256", job.cover_letter_sha256),
            **kw,
        )

    def to_dict(self) -> dict:
        """Serialize to an application-result.v1 document."""
        return {
            "schema": APPLICATION_RESULT_SCHEMA_ID,
            "executor_run_id": self.executor_run_id,
            "integration_id": self.integration_id,
            "canonical_url": self.canonical_url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "confidence": self.confidence,
            "company": self.company,
            "role": self.role,
            "career_ops_score": self.career_ops_score,
            "resume_path": self.resume_path,
            "cover_letter_path": self.cover_letter_path,
            "screenshot_paths": self.screenshot_paths,
            "questions_encountered": self.questions_encountered,
            "fields_filled": self.fields_filled,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "manual_action_required": self.manual_action_required,
            "resume_sha256": self.resume_sha256,
            "cover_letter_sha256": self.cover_letter_sha256,
        }
