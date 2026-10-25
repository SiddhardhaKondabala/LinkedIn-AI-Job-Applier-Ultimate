"""Policy and page-signal detection for queue-mode execution (Phase 7).

Two pure, browser-free concerns live here so they can be unit-tested without a
page:

1. ``classify_question`` — decide whether a form question is one the bot must
   NOT auto-answer (work authorization, sponsorship, security clearance, legal
   history, demographic/EEO, salary expectations). career-ops is a
   human-in-the-loop tool; these go back to the person as ``manual_review``.

2. ``detect_challenge`` — decide, from signals read off the live page (URL,
   visible text, element markers), whether LinkedIn has interrupted the flow
   with a captcha, an MFA/2FA prompt, or a security checkpoint. This replaces
   the Phase 3 heuristic that guessed from a free-text reason string.

The browser layer (easy applier) reads the raw signals and calls these; the
integration adapter maps the resulting codes onto application-result.v1 values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

# --- sensitive-question policy ---------------------------------------------

#: Ordered (category, phrases) — first category whose phrase appears wins, so a
#: more specific/serious category is listed before a broader one. Phrases are
#: matched case-insensitively as substrings of the question text.
_SENSITIVE_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "work_authorization",
        (
            "work authorization",
            "work authorisation",
            "authorized to work",
            "authorised to work",
            "legally authorized",
            "legally authorised",
            "right to work",
            "eligible to work",
        ),
    ),
    (
        "sponsorship",
        (
            "sponsorship",
            "require sponsorship",
            "need sponsorship",
            "visa",
            "work permit",
            "immigration status",
        ),
    ),
    (
        "security_clearance",
        (
            "security clearance",
            "clearance level",
            "sci clearance",
            "ts/sci",
            "secret clearance",
        ),
    ),
    (
        "legal_history",
        (
            "criminal",
            "convicted",
            "conviction",
            "felony",
            "misdemeanor",
            "background check",
        ),
    ),
    (
        "demographic",
        (
            "gender",
            "ethnicity",
            "race",
            "disability",
            "veteran",
            "sexual orientation",
            "equal employment",
            "eeo",
        ),
    ),
    (
        "salary_expectation",
        (
            "salary expectation",
            "expected salary",
            "desired salary",
            "compensation expectation",
            "expected compensation",
            "desired compensation",
        ),
    ),
)


#: The sensitive-question categories, in priority order. Public so the
#: sensitive-answers config can validate its keys against them.
SENSITIVE_CATEGORIES: tuple[str, ...] = tuple(c for c, _ in _SENSITIVE_CATEGORIES)


def classify_question(text: Optional[str]) -> Optional[str]:
    """Return the sensitive-question category for ``text``, or None.

    A non-None result means the bot must stop and hand this back for manual
    review instead of auto-answering it.
    """
    if not text:
        return None
    t = text.lower()
    for category, phrases in _SENSITIVE_CATEGORIES:
        if any(p in t for p in phrases):
            return category
    return None


# --- page-interruption (challenge) detection -------------------------------


@dataclass(frozen=True)
class Challenge:
    """A page interruption that blocks automated applying.

    ``kind`` is one of ``captcha``, ``requires_mfa``, ``security_checkpoint``;
    ``detail`` is a short human-readable note for the result's error message.
    """

    kind: str
    detail: str


#: URL fragments that, on their own, mean LinkedIn has diverted to a challenge.
_CHALLENGE_URL_MARKERS = (
    "/checkpoint/challenge",
    "/checkpoint/lg/",
    "/checkpoint/rp/",
)

_CAPTCHA_TEXT = (
    "captcha",
    "i'm not a robot",
    "im not a robot",
    "recaptcha",
    "hcaptcha",
    "are you a human",
    "verify you are human",
)
_MFA_TEXT = (
    "two-factor",
    "two factor",
    "2fa",
    "verification code",
    "enter the code",
    "authentication code",
    "one-time code",
    "one time passcode",
    "otp",
)
_CHECKPOINT_TEXT = (
    "security verification",
    "quick security check",
    "let's do a quick security check",
    "unusual activity",
    "confirm your identity",
    "verify your identity",
    "verify it's you",
)


def detect_challenge(
    *,
    url: Optional[str] = None,
    text: Optional[str] = None,
    markers: Optional[Iterable[str]] = None,
) -> Optional[Challenge]:
    """Classify a page interruption from signals read off the live page.

    - ``url``: the current page URL.
    - ``text``: visible page text (lower/mixed case both fine).
    - ``markers``: names of challenge elements the browser probe found present,
      e.g. ``"captcha_iframe"``, ``"mfa_input"``, ``"checkpoint_form"``.

    Captcha wins over MFA, which wins over a generic checkpoint, because the
    most specific interruption is the most actionable. Returns None when no
    challenge signal is present.
    """
    u = (url or "").lower()
    t = (text or "").lower()
    m = set(markers or ())

    if "captcha_iframe" in m or any(k in t for k in _CAPTCHA_TEXT):
        return Challenge("captcha", "captcha challenge detected")

    if "mfa_input" in m or any(k in t for k in _MFA_TEXT):
        return Challenge("requires_mfa", "two-factor / verification code required")

    checkpoint_url = any(k in u for k in _CHALLENGE_URL_MARKERS)
    if "checkpoint_form" in m or checkpoint_url or any(k in t for k in _CHECKPOINT_TEXT):
        return Challenge("security_checkpoint", "security checkpoint / identity verification")

    return None
