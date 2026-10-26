"""Phase 3 security correction for the external apply agent.

The old task prompt constructed an account password from the email local-part
(`localpart + "123456"`) for account registration. That is a credential-handling
risk. Account registration must stop for manual user action, and no password may
be generated. These tests pin the corrected prompt.
"""

from unittest.mock import patch

import pytest

from src.llm.apply_agent import ApplyAgent


def _agent(tmp_path, user_email="jordan@example.test"):
    with patch.object(ApplyAgent, "select_model_type", return_value=None):
        agent = ApplyAgent(
            api_key="key",
            browser_storage_state=str(tmp_path / "state.json"),
            user_email=user_email,
        )
    agent.set_resume("RESUME TEXT")
    return agent


def test_task_never_constructs_a_password(tmp_path):
    task = _agent(tmp_path)._build_task("https://www.linkedin.com/jobs/view/123", "/abs/resume.pdf")
    assert "123456" not in task
    assert "jordan123456" not in task
    # The email is no longer injected into the prompt at all.
    assert "jordan@example.test" not in task


def test_account_registration_becomes_a_manual_stop(tmp_path):
    task = (
        _agent(tmp_path)
        ._build_task("https://www.linkedin.com/jobs/view/123", "/abs/resume.pdf")
        .lower()
    )
    assert "register or create an account" in task
    assert "do not create one" in task
    assert "manually" in task


def test_resume_path_still_present(tmp_path):
    task = _agent(tmp_path)._build_task("https://www.linkedin.com/jobs/view/123", "/abs/resume.pdf")
    assert "/abs/resume.pdf" in task


def test_queue_external_prompt_requires_explicit_outcome_and_manual_gates(tmp_path):
    task = (
        _agent(tmp_path)
        ._build_task(
            "https://external.example/apply",
            "/abs/resume.pdf",
            "/abs/cover.pdf",
            manual_review_gate=True,
        )
        .lower()
    )
    assert "/abs/cover.pdf" in task
    assert "outcome: submitted" in task
    assert "visible submission confirmation" in task
    assert "work-authorization" in task


@pytest.mark.asyncio
async def test_successful_agent_run_without_submission_sentinel_fails_closed(tmp_path):
    agent = _agent(tmp_path)
    with patch.object(
        agent, "apply", return_value="Could not apply; account registration required"
    ):
        status, reason = await agent.apply_to_job("https://external.example/apply")
    assert status == "Error"
    assert "did not confirm submission" in reason


@pytest.mark.asyncio
async def test_only_explicit_submitted_sentinel_reports_success(tmp_path):
    agent = _agent(tmp_path)
    with patch.object(agent, "apply", return_value="Done\nOUTCOME: submitted"):
        status, reason = await agent.apply_to_job("https://external.example/apply")
    assert (status, reason) == ("Success", "")


@pytest.mark.asyncio
async def test_submitted_sentinel_must_be_the_final_line(tmp_path):
    agent = _agent(tmp_path)
    with patch.object(
        agent,
        "apply",
        return_value="Possible outcomes include:\nOUTCOME: submitted\nNo application was sent.",
    ):
        status, reason = await agent.apply_to_job("https://external.example/apply")
    assert status == "Error"
    assert "did not confirm submission" in reason
