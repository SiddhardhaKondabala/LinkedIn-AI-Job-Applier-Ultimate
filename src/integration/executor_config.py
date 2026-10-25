"""Configuration for an approved-queue execution run.

Safety is the default: a final application is submitted only when ``submit`` is
explicitly set AND neither a dry run nor test mode is active. The CLI maps its
flags onto this; the runner reads ``should_submit``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class QueueExecutionConfig:
    executor_run_id: str
    min_score: float = 4.0
    max_jobs: Optional[int] = None
    #: Perform real submissions. Off by default — must be set explicitly.
    submit: bool = False
    #: Load/validate/plan only; never drive the browser.
    dry_run: bool = False
    #: Run the apply flow but never click the final submit.
    test_mode: bool = False

    @property
    def should_submit(self) -> bool:
        """Whether the executor may click the final submit button."""
        return bool(self.submit) and not self.dry_run and not self.test_mode
