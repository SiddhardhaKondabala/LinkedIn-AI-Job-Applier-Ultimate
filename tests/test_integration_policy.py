"""Phase 7 — sensitive-question policy + page-challenge detection (pure)."""

import pytest

from src.integration.policy import classify_question, detect_challenge


# --- classify_question -----------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Are you legally authorized to work in the US?", "work_authorization"),
        ("Do you now or in the future require visa sponsorship?", "sponsorship"),
        ("What is your security clearance level?", "security_clearance"),
        ("Have you ever been convicted of a felony?", "legal_history"),
        ("Please share your gender and ethnicity (EEO).", "demographic"),
        ("What is your expected salary?", "salary_expectation"),
    ],
)
def test_classify_question_flags_sensitive(text, expected):
    assert classify_question(text) == expected


def test_classify_question_ignores_ordinary_and_empty():
    assert classify_question("How many years of Python experience do you have?") is None
    assert classify_question("") is None
    assert classify_question(None) is None


@pytest.mark.parametrize(
    "text",
    [
        "Do you embrace our engineering culture?",   # 'race' inside 'embrace'
        "How close is the office to the racetrack?",  # 'race' inside 'racetrack'
        "Are you open to relocating to Visalia, CA?",  # 'visa' inside 'Visalia'
        "Describe a project you are proud of.",        # no sensitive token
    ],
)
def test_classify_question_does_not_false_positive_on_embedded_tokens(text):
    assert classify_question(text) is None


def test_classify_question_still_matches_whole_words_with_punctuation():
    assert classify_question("Do you hold a TS/SCI clearance?") == "security_clearance"
    assert classify_question("Are you a protected veteran?") == "demographic"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Do you require any visas to work here?", "sponsorship"),      # regular plural
        ("Are you one of our protected veterans?", "demographic"),      # regular plural
        ("Do you have any disabilities?", "demographic"),               # irregular plural
        ("Have you had any felonies?", "legal_history"),                # irregular plural
        ("Do you need sponsorships for multiple roles?", "sponsorship"),
    ],
)
def test_classify_question_matches_plurals(text, expected):
    assert classify_question(text) == expected


def test_work_authorization_wins_over_sponsorship_ordering():
    # A question naming both: the more fundamental category is reported.
    assert (
        classify_question("Are you authorized to work here, and do you need sponsorship?")
        == "work_authorization"
    )


# --- detect_challenge ------------------------------------------------------


def test_detect_captcha_by_text_and_marker():
    assert detect_challenge(text="Please complete the reCAPTCHA").kind == "captcha"
    assert detect_challenge(markers=["captcha_iframe"]).kind == "captcha"


def test_detect_mfa_by_text_and_marker():
    assert detect_challenge(text="Enter the verification code we sent").kind == "requires_mfa"
    assert detect_challenge(markers=["mfa_input"]).kind == "requires_mfa"


def test_detect_checkpoint_by_url_text_and_marker():
    assert (
        detect_challenge(url="https://www.linkedin.com/checkpoint/challenge/abc").kind
        == "security_checkpoint"
    )
    assert detect_challenge(text="Let's do a quick security check").kind == "security_checkpoint"
    assert detect_challenge(markers=["checkpoint_form"]).kind == "security_checkpoint"


def test_captcha_wins_over_mfa_and_checkpoint():
    c = detect_challenge(
        url="https://www.linkedin.com/checkpoint/challenge",
        text="enter the verification code",
        markers=["captcha_iframe", "mfa_input", "checkpoint_form"],
    )
    assert c.kind == "captcha"


def test_detect_challenge_none_on_a_normal_page():
    assert detect_challenge(url="https://www.linkedin.com/jobs/view/123", text="Apply now") is None
    assert detect_challenge() is None


# --- Phase 7: the easy-applier gate wiring (queue mode only) ---

from pathlib import Path  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from src.job_manager.easy_applier import ChallengeDetected, ManualReviewRequired  # noqa: E402

MOD = "src.job_manager.linkedin.easy_applier_linkedin"


def _applier(gate: bool):
    from src.job_manager.linkedin.easy_applier_linkedin import LinkedInEasyApplier

    with (
        patch(f"{MOD}.get_ready_made_resume", return_value=None),
        patch(f"{MOD}.get_ready_made_photo", return_value=None),
        patch.object(LinkedInEasyApplier, "_load_questions", return_value=[]),
    ):
        a = LinkedInEasyApplier(
            page=MagicMock(),
            gpt_answerer=MagicMock(),
            resume_anonymizer=MagicMock(),
            resume_generator_manager=MagicMock(selected_style=None),
            pause_checker=None,
            answers_file=Path("answers.yaml"),
            resume_dir=Path("resume"),
            cover_letter_dir=Path("cover"),
            test_mode=True,
        )
    a.set_manual_review_gate(gate)
    return a


@pytest.mark.asyncio
async def test_policy_gate_stops_on_sensitive_question():
    a = _applier(gate=True)
    section = MagicMock()
    section.text_content = AsyncMock(return_value="Do you require visa sponsorship?")
    with pytest.raises(ManualReviewRequired) as ei:
        await a._enforce_manual_review_policy(section)
    assert ei.value.reason == "sponsorship"


@pytest.mark.asyncio
async def test_policy_gate_is_noop_when_disabled():
    a = _applier(gate=False)
    section = MagicMock()
    section.text_content = AsyncMock(return_value="Do you require visa sponsorship?")
    await a._enforce_manual_review_policy(section)  # no raise


@pytest.mark.asyncio
async def test_challenge_probe_raises_on_captcha_marker():
    a = _applier(gate=True)
    a.page.url = "https://www.linkedin.com/jobs/view/1"
    with patch(f"{MOD}.find_element_safely", new=AsyncMock(return_value=object())):
        with pytest.raises(ChallengeDetected) as ei:
            await a._raise_if_challenged()
    assert ei.value.kind == "captcha"


@pytest.mark.asyncio
async def test_challenge_probe_noop_when_gate_disabled():
    a = _applier(gate=False)
    with patch(f"{MOD}.find_element_safely", new=AsyncMock(return_value=object())):
        await a._raise_if_challenged()  # no raise even if elements present


# --- Phase 7: configured sensitive answers auto-answer instead of stopping ---

from src.integration.sensitive_answers import load_sensitive_answers  # noqa: E402


def test_load_sensitive_answers_keeps_known_drops_unknown_and_blank(tmp_path):
    f = tmp_path / "sensitive.yaml"
    f.write_text(
        "work_authorization: Yes\n"
        "sponsorship: ''\n"           # blank -> dropped
        "made_up_category: foo\n"     # unknown -> dropped
        "salary_expectation: 150000\n",
        encoding="utf-8",
    )
    answers = load_sensitive_answers(str(f))
    assert answers == {"work_authorization": "Yes", "salary_expectation": "150000"}


def test_load_sensitive_answers_missing_file_is_empty(tmp_path):
    assert load_sensitive_answers(str(tmp_path / "nope.yaml")) == {}


@pytest.mark.asyncio
async def test_configured_sensitive_question_is_auto_answered_not_stopped():
    a = _applier(gate=True)
    a.set_sensitive_answers({"sponsorship": "No"})
    section = MagicMock()
    section.text_content = AsyncMock(return_value="Do you require visa sponsorship?")
    # No raise: the configured category proceeds.
    await a._enforce_manual_review_policy(section)
    # And the field handlers get the configured value via the cache lookup.
    cached = a._find_cached_question("Do you require visa sponsorship?", "radio")
    assert cached is not None
    assert cached.answer == "No"
    assert cached.question_type == "radio"


@pytest.mark.asyncio
async def test_unconfigured_sensitive_question_still_stops():
    a = _applier(gate=True)
    a.set_sensitive_answers({"salary_expectation": "150000"})  # different category
    section = MagicMock()
    section.text_content = AsyncMock(return_value="Do you require visa sponsorship?")
    with pytest.raises(ManualReviewRequired):
        await a._enforce_manual_review_policy(section)


def test_configured_answer_ignored_when_gate_disabled():
    a = _applier(gate=False)
    a.set_sensitive_answers({"salary_expectation": "150000"})
    # Gate off -> no sensitive injection, ordinary cache lookup (miss) returns None.
    assert a._find_cached_question("What is your expected salary?", "textbox") is None


@pytest.mark.asyncio
async def test_queue_mode_stops_for_manual_review_when_no_approved_cover_letter():
    # Review fix: never generate an unvetted LLM cover letter in queue mode.
    a = _applier(gate=True)
    a.set_pinned_artifacts(resume_path="/abs/cv.pdf", cover_letter_path=None)
    job = MagicMock(company_name="Acme", job_title="Staff Engineer")
    with pytest.raises(ManualReviewRequired) as ei:
        await a._create_and_upload_cover_letter(MagicMock(), job)
    assert ei.value.reason == "cover_letter_required"
    # The LLM cover-letter generator must not have been called.
    a.gpt_answerer.write_cover_letter.assert_not_called()


@pytest.mark.asyncio
async def test_standalone_still_generates_cover_letter():
    # Gate off (standalone search): behaviour unchanged — it does NOT raise for
    # manual review; it proceeds to the LLM generator. A sentinel on the
    # generator proves we reached it (and avoids writing a real PDF).
    a = _applier(gate=False)
    job = MagicMock(company_name="Acme", job_title="Staff Engineer")

    class _Reached(Exception):
        pass

    a.gpt_answerer.write_cover_letter.side_effect = _Reached
    with pytest.raises(_Reached):
        await a._create_and_upload_cover_letter(MagicMock(), job)
