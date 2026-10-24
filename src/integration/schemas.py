"""Versioned bridge contract (career-ops <-> LinkedIn bot).

career-ops owns the canonical JSON Schemas in its ``schemas/`` directory. This
package vendors byte-for-byte copies under ``schemas/`` and validates documents
with the project's existing ``jsonschema`` dependency. When a schema version
bumps, update both copies together (see career-ops docs/INTEGRATION_LINKEDIN.md).

Importing this module has no side effects beyond reading the vendored schema
files, so it is safe to import from standalone code paths.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator

_SCHEMA_DIR = Path(__file__).parent / "schemas"

APPROVED_JOBS_SCHEMA_ID = "approved-jobs.v1"
APPLICATION_RESULT_SCHEMA_ID = "application-result.v1"

#: Evaluation / result confidence levels, ordered low -> high.
CONFIDENCE_LEVELS = ("low", "medium", "high")

#: Allowed values of a result record's ``result`` field.
RESULT_VALUES = (
    "submitted",
    "already_applied",
    "manual_review",
    "captcha",
    "requires_mfa",
    "failed",
    "skipped",
    "expired",
    "invalid",
)

#: The only result that means an application was actually sent.
SUBMITTED_RESULT = "submitted"


def _load(name: str) -> dict[str, Any]:
    return json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))


APPROVED_JOBS_SCHEMA = _load("approved-jobs.v1.schema.json")
APPLICATION_RESULT_SCHEMA = _load("application-result.v1.schema.json")

_APPROVED_VALIDATOR = Draft7Validator(APPROVED_JOBS_SCHEMA)
_RESULT_VALIDATOR = Draft7Validator(APPLICATION_RESULT_SCHEMA)


class SchemaError(ValueError):
    """Raised when a bridge document violates its schema.

    ``errors`` holds the human-readable validation messages.
    """

    def __init__(self, message: str, errors: list[str]):
        super().__init__(f"{message}:\n  " + "\n  ".join(errors))
        self.errors = errors


def _format_path(parts) -> str:
    out = "$"
    for p in parts:
        out += f"[{p}]" if isinstance(p, int) else f".{p}"
    return out


def _collect(validator: Draft7Validator, doc: Any) -> list[str]:
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    return [f"{_format_path(e.absolute_path)}: {e.message}" for e in errors]


def validate_approved_jobs(doc: Any) -> list[str]:
    """Return a list of validation error strings (empty when valid)."""
    return _collect(_APPROVED_VALIDATOR, doc)


def validate_application_result(doc: Any) -> list[str]:
    """Return a list of validation error strings (empty when valid)."""
    return _collect(_RESULT_VALIDATOR, doc)


def assert_approved_jobs(doc: Any) -> Any:
    errors = validate_approved_jobs(doc)
    if errors:
        raise SchemaError(
            f"approved-jobs document failed {APPROVED_JOBS_SCHEMA_ID} validation", errors
        )
    return doc


def assert_application_result(doc: Any) -> Any:
    errors = validate_application_result(doc)
    if errors:
        raise SchemaError(
            f"application-result document failed {APPLICATION_RESULT_SCHEMA_ID} validation",
            errors,
        )
    return doc
