"""Durable, idempotent writer for application-result.v1 records (Phase 4).

Writes one JSONL record per job attempt, incrementally, so an interrupted run
still leaves durable records for the jobs it finished. The idempotency key is
``(executor_run_id, integration_id)`` — NOT company+role — so re-running the
same run (e.g. resuming after an interruption) never duplicates a record, while
a new run appends its own records to the log.

Each record is validated against application-result.v1 before it is written, so
a malformed result can never reach the file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .schemas import assert_application_result


class ResultWriter:
    """Append application results to a JSONL file, once per (run, job)."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._seen: set[tuple] = self._load_existing_keys()

    def _load_existing_keys(self) -> set:
        keys: set = set()
        if not self.path.exists():
            return keys
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a partially written trailing line
            keys.add((d.get("executor_run_id"), d.get("integration_id")))
        return keys

    def append(self, result) -> bool:
        """Write one result. Returns False if this (run, job) was already written."""
        d = result.to_dict() if hasattr(result, "to_dict") else result
        assert_application_result(d)  # never write a record that violates the contract
        key = (d["executor_run_id"], d["integration_id"])
        if key in self._seen:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(d) + "\n")
        self._seen.add(key)
        return True

    def append_all(self, results: Iterable) -> int:
        """Append several results; return how many were newly written."""
        return sum(1 for r in results if self.append(r))
