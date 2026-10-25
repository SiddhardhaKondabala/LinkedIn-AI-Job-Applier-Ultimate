"""Phase 3 — raw apply outcome -> result value mapping."""

from src.integration.outcome_mapping import classify_outcome


def test_success_is_submitted():
    c = classify_outcome("Success", "")
    assert c.result == "submitted"
    assert c.error_code is None
    assert c.manual_action_required is False


def test_already_applied_by_flag():
    assert classify_outcome("Success", "", already_applied=True).result == "already_applied"


def test_already_applied_by_reason():
    assert classify_outcome("Skip", "Already applied to this job").result == "already_applied"


def test_test_mode_skip():
    c = classify_outcome("Skip", "Test mode")
    assert c.result == "skipped"
    assert c.error_code == "test_mode"


def test_generic_skip():
    assert classify_outcome("Skip", "no easy apply button").result == "skipped"


def test_error_is_failed():
    assert classify_outcome("Error", "boom").result == "failed"


def test_limit_is_skipped():
    assert classify_outcome("Limit", "daily limit reached").result == "skipped"


def test_captcha_detected():
    c = classify_outcome("Error", "A CAPTCHA was presented")
    assert c.result == "captcha"
    assert c.manual_action_required is True


def test_mfa_detected():
    assert classify_outcome("Error", "Security check / verification required").result == "requires_mfa"


def test_manual_review_for_legal():
    assert classify_outcome("Skip", "Requires work authorization / sponsorship").result == "manual_review"


def test_unknown_status_is_failed():
    assert classify_outcome("weird", "").result == "failed"
