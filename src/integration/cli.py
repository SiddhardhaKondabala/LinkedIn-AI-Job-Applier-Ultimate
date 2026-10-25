"""Queue-mode CLI argument parsing.

Kept out of main.py so it imports nothing heavy (no browser/LLM), and can be
unit-tested directly.
"""

from __future__ import annotations

import argparse
import uuid

from .executor_config import QueueExecutionConfig


def parse_queue_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LinkedIn bot (search or queue mode)")
    parser.add_argument(
        "--approved-jobs",
        dest="approved_jobs",
        help="Path to a career-ops approved-jobs.v1 queue. Presence enables queue mode.",
    )
    parser.add_argument("--results", help="Path to write application-result.v1 JSONL.")
    parser.add_argument("--executor-run-id", dest="executor_run_id")
    parser.add_argument("--min-score", dest="min_score", type=float)
    parser.add_argument("--max-jobs", dest="max_jobs", type=int)
    parser.add_argument("--submit", action="store_true", help="Perform real submissions.")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="Validate/plan only; no browser.")
    parser.add_argument("--test-mode", dest="test_mode", action="store_true", help="Fill but never submit.")
    parser.add_argument("--allow-external", dest="allow_external", action="store_true")
    # Ignore unknown args so the dashboard/other launchers can pass extras.
    return parser.parse_known_args(argv)[0]


def queue_config_from_args(args, default_min_score: float = 4.0) -> QueueExecutionConfig:
    run_id = getattr(args, "executor_run_id", None) or f"run-{uuid.uuid4().hex[:12]}"
    return QueueExecutionConfig(
        executor_run_id=run_id,
        min_score=args.min_score if args.min_score is not None else default_min_score,
        max_jobs=args.max_jobs,
        submit=bool(args.submit),
        dry_run=bool(args.dry_run),
        test_mode=bool(args.test_mode),
    )
