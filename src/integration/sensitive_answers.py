"""User-configured standing answers to sensitive questions (Phase 7).

Sensitive questions (work authorization, sponsorship, clearance, legal history,
demographic/EEO, salary expectations) are facts about the candidate that barely
change job to job. When the manual-review gate meets one, the bot auto-answers
it from this config instead of stopping — so the only questions that still need
a human are the ones the candidate has NOT pre-answered.

The config is a flat YAML map of ``category: answer``, where category is one of
``policy.SENSITIVE_CATEGORIES`` and answer is the candidate's own value
(e.g. ``work_authorization: "Yes"``, ``sponsorship: "No"``,
``salary_expectation: "150000"``). It is user data and lives outside git, like
the rest of the bot's ``config/``. Unknown keys and blank values are dropped
with a warning; a missing file is simply an empty map (gate still stops on every
sensitive question).
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from config.logger_config import logger

from .policy import SENSITIVE_CATEGORIES

#: Default location; override with the CAREER_OPS_SENSITIVE_ANSWERS env var.
DEFAULT_SENSITIVE_ANSWERS_FILE = "config/sensitive_answers.yaml"


def sensitive_answers_path() -> str:
    """Resolve the config path (env override wins)."""
    return os.environ.get("CAREER_OPS_SENSITIVE_ANSWERS", DEFAULT_SENSITIVE_ANSWERS_FILE)


def load_sensitive_answers(path: str | None = None) -> dict[str, str]:
    """Load the category->answer map; return {} when the file is absent.

    Keeps only known categories with a non-empty value, coercing each value to
    a trimmed string. A malformed or non-mapping file logs a warning and yields
    an empty map, so a bad config never auto-answers anything.
    """
    p = Path(path or sensitive_answers_path())
    if not p.is_file():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        logger.warning(f"Ignoring malformed sensitive-answers config {p}: {e}")
        return {}
    if not isinstance(raw, dict):
        logger.warning(f"Sensitive-answers config {p} is not a mapping; ignoring it")
        return {}

    answers: dict[str, str] = {}
    for key, value in raw.items():
        category = str(key).strip()
        if category not in SENSITIVE_CATEGORIES:
            logger.warning(f"Ignoring unknown sensitive-answer category '{category}' in {p}")
            continue
        # YAML parses bare Yes/No/True/False as booleans; map them back to the
        # Yes/No labels these questions actually use on the form.
        if isinstance(value, bool):
            text = "Yes" if value else "No"
        else:
            text = "" if value is None else str(value).strip()
        if not text:
            logger.warning(f"Ignoring blank sensitive-answer for '{category}' in {p}")
            continue
        answers[category] = text
    if answers:
        logger.info(
            f"Loaded {len(answers)} configured sensitive answer(s): {', '.join(sorted(answers))}"
        )
    return answers
