from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.llm.apply_agent import ApplyAgent


def _make_history(successful: bool | None, final_result: str | None):
    history = MagicMock()
    history.is_successful.return_value = successful
    history.final_result.return_value = final_result
    return history


async def _apply_with_history(tmp_path, history):
    agent_instance = MagicMock()
    agent_instance.run = AsyncMock(return_value=history)
    browser_instance = MagicMock()
    browser_instance.stop = AsyncMock()

    apply_agent = ApplyAgent(api_key="key", browser_storage_state=str(tmp_path / "state.json"))
    apply_agent.set_resume("resume text")

    with (
        patch("src.llm.apply_agent.Agent", return_value=agent_instance),
        patch.object(ApplyAgent, "_create_browser", return_value=browser_instance),
        patch.object(ApplyAgent, "_log_token_usage"),
        patch("src.llm.apply_agent.get_ready_made_resume", return_value=tmp_path / "resume.pdf"),
        patch("src.llm.apply_agent.emit_event"),
    ):
        return await apply_agent.apply_to_job("https://example.com/job")


@pytest.mark.asyncio
async def test_apply_to_job_returns_success_when_agent_succeeds(tmp_path):
    result = await _apply_with_history(
        tmp_path, _make_history(True, "Submitted\nOUTCOME: submitted")
    )
    assert result == ("Success", "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "successful, final_result, expected_reason",
    [
        (False, "Page shows a server error", "Page shows a server error"),
        (None, None, "Apply agent did not complete the application"),
    ],
)
async def test_apply_to_job_returns_error_when_agent_fails(
    tmp_path, successful, final_result, expected_reason
):
    result = await _apply_with_history(tmp_path, _make_history(successful, final_result))
    assert result == ("Error", expected_reason)
