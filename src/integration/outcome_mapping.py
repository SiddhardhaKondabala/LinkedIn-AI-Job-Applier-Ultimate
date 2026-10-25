"""Map a raw apply outcome to an application-result.v1 ``result`` value.

The LinkedIn job manager returns a plain ``(status, reason, already_applied)``
outcome from applying to one job. This module turns that into the canonical
result value, keeping the browser code free of the bridge vocabulary.

Phase 7 adds explicit, page-detected statuses — ``ManualReview`` (a sensitive
question the bot must not auto-answer) and ``Challenge`` (a captcha/MFA/security
checkpoint) — which carry a precise code and take precedence over the free-text
heuristics kept below as a fallback for the non-Easy-Apply agent path.
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

    # Phase 7 explicit, page-detected signals win over everything else.
    if s == "manualreview":
        # reason is the policy category (e.g. "work_authorization").
        return Classification("manual_review", True, r or "manual_review")
    if s == "challenge":
        # reason is the challenge kind: captcha / requires_mfa / security_checkpoint.
        if r == "captcha":
            return Classification("captcha", True, "captcha")
        # A checkpoint or verification step needs the person — treat as MFA,
        # preserving the precise kind as the error code.
        return Classification("requires_mfa", True, r or "requires_mfa")

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
