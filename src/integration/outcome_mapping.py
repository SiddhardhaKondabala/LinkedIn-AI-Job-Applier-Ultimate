"""Map a raw apply outcome to an application-result.v1 ``result`` value.

The LinkedIn job manager returns a plain ``(status, reason, already_applied)``
outcome from applying to one job. This module turns that into the canonical
result value, keeping the browser code free of the bridge vocabulary. Captcha /
MFA / legal-question detection is heuristic for now (over the reason text);
Phase 7 strengthens the actual detection.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Classification:
    result: str
    manual_action_required: bool
    error_code: Optional[str]


_MFA_HINTS = (
    "verification",
    "verify your",
    "2fa",
    "two-factor",
    "two factor",
    "mfa",
    "security check",
    "confirm your identity",
)
_MANUAL_HINTS = (
    "work authoriz",
    "sponsorship",
    "visa",
    "legal",
    "demographic",
    "salary expectation",
    "unknown required",
    "manual review",
    "external apply",
)


def classify_outcome(status: str, reason: str = "", already_applied: bool = False) -> Classification:
    """Classify a raw apply outcome into a result value.

    Order matters: already-applied and the hard-stop signals (captcha/MFA/manual
    review) win over the coarse status, because they can occur with any status.
    """
    s = (status or "").strip().lower()
    r = (reason or "").strip().lower()

    if already_applied or "already applied" in r:
        return Classification("already_applied", False, None)
    if "captcha" in r:
        return Classification("captcha", True, "captcha")
    if any(h in r for h in _MFA_HINTS):
        return Classification("requires_mfa", True, "requires_mfa")
    if any(h in r for h in _MANUAL_HINTS):
        return Classification("manual_review", True, "manual_review")

    if s == "success":
        return Classification("submitted", False, None)
    if s == "limit":
        return Classification("skipped", False, "limit")
    if s == "skip":
        return Classification("skipped", False, "test_mode" if "test mode" in r else "skipped")
    if s == "error":
        return Classification("failed", False, "apply_error")

    return Classification("failed", False, "unknown_outcome")
