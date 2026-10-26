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
An abandoned in-progress claim expires after a configurable timeout (24 hours
by default); a finalized submission or human-needed stop is terminal and never
expires.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from .filelock import file_lock
from .queue_models import ApprovedJob, utc_now_iso

DEFAULT_CLAIM_TIMEOUT_SECONDS = 24 * 60 * 60


def configured_claim_timeout_seconds() -> float:
    """Configured claim TTL; invalid values fall back to the safe 24h default.

    Set ``CAREER_OPS_CLAIM_TIMEOUT_SECONDS=0`` to disable expiration explicitly.
    """
    raw = os.environ.get("CAREER_OPS_CLAIM_TIMEOUT_SECONDS")
    if raw is None:
        return float(DEFAULT_CLAIM_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except ValueError:
        return float(DEFAULT_CLAIM_TIMEOUT_SECONDS)
    return max(0.0, value)


class ClaimStore:
    """Atomic, filesystem-backed claims keyed by a job's integration_id."""

    def __init__(self, directory: str, timeout_seconds: Optional[float] = None):
        self.dir = Path(directory)
        self.timeout_seconds = (
            configured_claim_timeout_seconds()
            if timeout_seconds is None
            else max(0.0, timeout_seconds)
        )

    def _path(self, integration_id: str) -> Path:
        # Hash the id so any id (it contains ':' and URL characters) maps to a
        # safe, fixed-length filename.
        name = hashlib.sha256(integration_id.encode("utf-8")).hexdigest()
        return self.dir / f"{name}.claim"

    def _lock_path(self, integration_id: str) -> Path:
        return self._path(integration_id).with_suffix(".lock")

    def try_claim(self, integration_id: str, run_id: str) -> bool:
        """Atomically claim a job. True if acquired, False if already claimed."""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(integration_id)
        # The separate lock makes stale-claim reclamation atomic as a whole.
        # O_EXCL alone protects a fresh create, but without this lock two
        # reclaimers can both observe an old file and one can unlink the
        # other's newly-created claim.
        with file_lock(self._lock_path(integration_id)):
            if path.exists():
                if not self._is_expired(path):
                    return False
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                return False
            try:
                payload = json.dumps(
                    {
                        "integration_id": integration_id,
                        "run_id": run_id,
                        "claimed_at": utc_now_iso(),
                    }
                )
                os.write(fd, payload.encode("utf-8"))
            finally:
                os.close(fd)
            return True

    def release(self, integration_id: str) -> None:
        """Drop a claim so a later run may retry the job."""
        with file_lock(self._lock_path(integration_id)):
            try:
                self._path(integration_id).unlink()
            except FileNotFoundError:
                pass

    def finalize(self, integration_id: str, result: str) -> None:
        """Mark a kept claim terminal so TTL cleanup can never reopen it."""
        path = self._path(integration_id)
        with file_lock(self._lock_path(integration_id)):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                return
            data.update({"state": "terminal", "result": result, "finished_at": utc_now_iso()})
            tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with open(tmp, "x", encoding="utf-8") as fh:
                    os.chmod(tmp, 0o600)
                    json.dump(data, fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass

    def is_claimed(self, integration_id: str) -> bool:
        """True when a claim file exists for the job (held by some run)."""
        path = self._path(integration_id)
        with file_lock(self._lock_path(integration_id)):
            if not path.exists():
                return False
            if self._is_expired(path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                return False
            return True

    def owner(self, integration_id: str) -> Optional[str]:
        """The run_id that holds the claim, or None — for diagnostics."""
        path = self._path(integration_id)
        with file_lock(self._lock_path(integration_id)):
            if not path.exists() or self._is_expired(path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                return None
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                return None
            return data.get("run_id")

    def _is_expired(self, path: Path) -> bool:
        if self.timeout_seconds <= 0:
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("state") == "terminal":
                return False
            age = time.time() - path.stat().st_mtime
        except FileNotFoundError:
            return True
        except json.JSONDecodeError:
            age = time.time() - path.stat().st_mtime
        return age >= self.timeout_seconds


#: Outcomes that represent a real application attempt or a human-needed stop —
#: the claim is KEPT so no run applies to the job again. Any other post-apply
#: outcome (notably ``failed``) releases the claim so a later run can retry.
#:
#: Caveat: a failure raised AFTER the form was actually submitted (e.g. an error
#: reading the confirmation) is classified ``failed`` and releases the claim, so
#: a retry could submit a second time. The executor avoids this by classifying a
#: completed submit as ``submitted`` (kept); the residual risk is only a crash
#: in the narrow window after submit and before the result is recorded.
KEEP_CLAIM_RESULTS = frozenset(
    {"submitted", "already_applied", "manual_review", "captcha", "requires_mfa", "expired"}
)


def claimant_from_store(store: ClaimStore):
    """Build the (claim, release, finalize) runner callables bound to a store."""

    def claim(job: ApprovedJob, run_id: str) -> bool:
        return store.try_claim(job.integration_id, run_id)

    def release(job: ApprovedJob) -> None:
        store.release(job.integration_id)

    def finalize(job: ApprovedJob, result: str) -> None:
        store.finalize(job.integration_id, result)

    return claim, release, finalize
