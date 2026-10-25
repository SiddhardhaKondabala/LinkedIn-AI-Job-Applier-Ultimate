"""Load and validate the career-ops approved-job queue.

The queue is validated against approved-jobs.v1 BEFORE any browser work, so an
unsupported version or a malformed queue fails fast rather than after logging in.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

from .queue_models import ApprovedJob
from .schemas import assert_approved_jobs


class QueueError(ValueError):
    """Raised when the queue file is missing or not valid JSON."""


def load_queue(path: str) -> Tuple[dict, list[ApprovedJob]]:
    """Return (envelope, jobs). Raises QueueError or SchemaError on bad input."""
    p = Path(path)
    if not p.exists():
        raise QueueError(f"approved-jobs queue not found: {p}")
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise QueueError(f"approved-jobs queue is not valid JSON ({p}): {e}")
    assert_approved_jobs(doc)  # raises schemas.SchemaError before any browser work
    jobs = [ApprovedJob.from_dict(j) for j in doc["jobs"]]
    return doc, jobs


def queue_min_score(doc: dict) -> float:
    """The queue's declared minimum score."""
    return float(doc["min_score"])
