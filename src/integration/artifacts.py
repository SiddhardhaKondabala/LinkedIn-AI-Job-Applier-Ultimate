"""Artifact handoff: use the EXACT career-ops resume/cover letter (Phase 6).

career-ops tailors a resume (and optionally a cover letter) per job and names
the files in the approved-jobs queue, with a SHA-256 of each. The executor must
upload those exact files — never a bot-generated or stale one — so this module
resolves each path to an absolute location, requires its approval hash, and
confirms the bytes still match what career-ops approved.

This is a pure, browser-free module: the runner gates on it per job, and a
preflight runs it across the whole queue BEFORE the browser starts, so a missing
or swapped artifact is caught up front instead of mid-login.

Queues produced by a pre-hardening exporter may not carry hashes. They fail
closed with ``manual_review`` here and must be regenerated before execution.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from .queue_models import ApprovedJob


class ArtifactError(Exception):
    """A queued artifact is missing or does not match its approved hash.

    ``error_code`` maps to an application-result.v1 error code, ``result`` to a
    result value, and ``manual_action_required`` is True throughout — a missing
    or changed artifact is a person's problem to resolve, not a retry.
    """

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        result: str = "invalid",
        manual_action_required: bool = True,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.result = result
        self.manual_action_required = manual_action_required


@dataclass(frozen=True)
class ResolvedArtifacts:
    """Verified, absolute artifact paths ready to hand to the uploader."""

    resume_path: str
    cover_letter_path: Optional[str]


def sha256_file(path: str) -> str:
    """Hex SHA-256 of a file's bytes, read in chunks to bound memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_one(path: str, expected_sha256: Optional[str], *, kind: str) -> str:
    """Resolve ``path`` to absolute, require an approval hash, and verify it."""
    abs_path = os.path.abspath(path)
    if not Path(abs_path).is_file():
        # A named-but-absent artifact is a broken queue, not a decision to make.
        raise ArtifactError(
            f"missing_{kind}",
            f"{kind} artifact not found: {abs_path}",
            result="invalid",
        )
    if not expected_sha256:
        raise ArtifactError(
            f"missing_{kind}_hash",
            f"{kind} artifact has no approval hash: {abs_path}",
            result="manual_review",
        )
    actual = sha256_file(abs_path)
    if actual != expected_sha256:
        # The bytes differ from what career-ops approved — never upload a
        # file the system of record did not vet; stop for manual review.
        raise ArtifactError(
            f"{kind}_hash_mismatch",
            f"{kind} artifact changed since approval "
            f"(expected {expected_sha256[:12]}…, got {actual[:12]}…): {abs_path}",
            result="manual_review",
        )
    return abs_path


def resolve_job_artifacts(job: ApprovedJob) -> ResolvedArtifacts:
    """Resolve and verify a job's resume and (optional) cover letter.

    Raises ArtifactError on the first problem. The resume is required; the cover
    letter is verified only when the queue names one.
    """
    resume_abs = _verify_one(job.resume_path, job.resume_sha256, kind="resume")
    cover_abs = None
    if job.cover_letter_path:
        cover_abs = _verify_one(job.cover_letter_path, job.cover_letter_sha256, kind="cover_letter")
    return ResolvedArtifacts(resume_path=resume_abs, cover_letter_path=cover_abs)


def preflight_artifacts(jobs: Iterable[ApprovedJob]) -> list[tuple[ApprovedJob, ArtifactError]]:
    """Verify every job's artifacts up front; return (job, error) for each problem.

    Returns an empty list when all artifacts resolve. Callers log the problems
    before launching the browser; the per-job runner gate still records a durable
    result for each so the good jobs in the queue still run.
    """
    problems: list[tuple[ApprovedJob, ArtifactError]] = []
    for job in jobs:
        try:
            resolve_job_artifacts(job)
        except ArtifactError as e:
            problems.append((job, e))
    return problems
