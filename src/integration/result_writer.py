"""Durable, idempotent writer for application-result.v1 records (Phase 4/8).

Writes one JSONL record per job attempt, incrementally, so an interrupted run
still leaves durable records for the jobs it finished. The idempotency key is
``(executor_run_id, integration_id)`` — NOT company+role — so re-running the
same run (e.g. resuming after an interruption) never duplicates a record, while
a new run appends its own records to the log.

Each record is validated against application-result.v1 before it is written, so
a malformed result can never reach the file.

Phase 8 — concurrency + durability: every append runs under an exclusive file
lock, refreshes its view of already-written keys from disk under that lock (so a
second process or a resumed run never writes a duplicate it couldn't see at
start-up), and fsyncs the line before releasing the lock (so an interruption
cannot leave a torn or lost record).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from .filelock import file_lock
from .schemas import assert_application_result


class ResultWriter:
    """Append application results to a JSONL file, once per (run, job)."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._synced_size = 0
        self._seen: set[tuple] = set()
        self._refresh_seen()

    def _refresh_seen(self) -> None:
        """Reload keys from disk when the file has grown since the last sync.

        Called under the lock before each write, so keys another writer appended
        after this instance started are seen and never duplicated.
        """
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            self._synced_size = 0
            return
        if size == self._synced_size and self._seen:
            return
        keys: set = set()
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a partially written trailing line
                keys.add((d.get("executor_run_id"), d.get("integration_id")))
        self._seen = keys
        self._synced_size = size

    def append(self, result) -> bool:
        """Write one result. Returns False if this (run, job) was already written."""
        d = result.to_dict() if hasattr(result, "to_dict") else result
        assert_application_result(d)  # never write a record that violates the contract
        key = (d["executor_run_id"], d["integration_id"])
        if key in self._seen:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(self._lock_path):
            # Re-check under the lock: another process may have written this key
            # (or grown the file) between our start-up scan and now.
            self._refresh_seen()
            if key in self._seen:
                return False
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(d) + "\n")
                f.flush()
                os.fsync(f.fileno())
            self._seen.add(key)
            try:
                self._synced_size = self.path.stat().st_size
            except OSError:
                self._synced_size = 0
        return True

    def append_all(self, results: Iterable) -> int:
        """Append several results; return how many were newly written."""
        return sum(1 for r in results if self.append(r))
