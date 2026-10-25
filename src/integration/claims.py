"""Job claiming so the same job is never applied to twice (Phase 8).

Two executor processes can run against one queue, and a resumed run gets a fresh
``executor_run_id`` — so the result writer's per-run idempotency is not enough to
stop a double application. A claim is a durable, atomic marker keyed by a job's
``integration_id``: a run must acquire it before applying, and a run that cannot
acquire it (because another run holds it, or a previous run already attempted the
job) skips that job.

The claim file is created with ``O_CREAT | O_EXCL`` — the create either wins
atomically or fails because the file exists, with no lock needed for the claim
itself. A claim is kept for an outcome that represents a real attempt or a
human-needed stop, and released for a transient failure so a later run retries.
A claim left behind by a crash blocks reprocessing (the safe default: never
double-apply); clearing the claims directory re-opens those jobs.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Optional

from .queue_models import ApprovedJob, utc_now_iso


class ClaimStore:
    """Atomic, filesystem-backed claims keyed by a job's integration_id."""

    def __init__(self, directory: str):
        self.dir = Path(directory)

    def _path(self, integration_id: str) -> Path:
        # Hash the id so any id (it contains ':' and URL characters) maps to a
        # safe, fixed-length filename.
        name = hashlib.sha256(integration_id.encode("utf-8")).hexdigest()
        return self.dir / f"{name}.claim"

    def try_claim(self, integration_id: str, run_id: str) -> bool:
        """Atomically claim a job. True if acquired, False if already claimed."""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(integration_id)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return False
        try:
            payload = json.dumps(
                {"integration_id": integration_id, "run_id": run_id, "claimed_at": utc_now_iso()}
            )
            os.write(fd, payload.encode("utf-8"))
        finally:
            os.close(fd)
        return True

    def release(self, integration_id: str) -> None:
        """Drop a claim so a later run may retry the job."""
        try:
            self._path(integration_id).unlink()
        except FileNotFoundError:
            pass

    def owner(self, integration_id: str) -> Optional[str]:
        """The run_id that holds the claim, or None — for diagnostics."""
        try:
            data = json.loads(self._path(integration_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        return data.get("run_id")


#: Outcomes that represent a real application attempt or a human-needed stop —
#: the claim is KEPT so no run applies to the job again. Any other post-apply
#: outcome (notably ``failed``) releases the claim so a later run can retry.
KEEP_CLAIM_RESULTS = frozenset(
    {"submitted", "already_applied", "manual_review", "captcha", "requires_mfa", "expired"}
)


def claimant_from_store(store: ClaimStore):
    """Build the (claim, release) callables the runner takes, bound to a store."""

    def claim(job: ApprovedJob, run_id: str) -> bool:
        return store.try_claim(job.integration_id, run_id)

    def release(job: ApprovedJob) -> None:
        store.release(job.integration_id)

    return claim, release
