"""Phase 4 — durable, idempotent result writer (M3)."""

import json

import pytest

from src.integration.executor_config import QueueExecutionConfig
from src.integration.queue_models import ApplicationResult, ApprovedJob
from src.integration.queue_runner import process_queue
from src.integration.result_writer import ResultWriter
from src.integration.schemas import SchemaError

NOW = "2026-10-24T12:00:00Z"


def make_job(integration_id="linkedin:111", url=None, score=4.2):
    return ApprovedJob.from_dict(
        {
            "integration_id": integration_id,
            "canonical_url": url or f"https://www.linkedin.com/jobs/view/{integration_id.split(':')[-1]}",
            "company": "Acme",
            "role": "Staff Engineer",
            "career_ops_score": score,
            "evaluation_confidence": "high",
            "report_number": 1,
            "report_path": "reports/1.md",
            "resume_path": "/abs/resume.pdf",
            "cover_letter_path": None,
            "source": "linkedin",
            "application_status": "approved",
            "created_at": "2026-10-24T12:00:00Z",
        }
    )


def make_result(run_id="run-1", integration_id="linkedin:111", result="submitted"):
    return ApplicationResult.for_job(
        make_job(integration_id=integration_id),
        executor_run_id=run_id,
        result=result,
        started_at=NOW,
        finished_at=NOW,
    )


def _lines(path):
    return path.read_text(encoding="utf-8").strip().splitlines() if path.exists() else []


def test_append_writes_valid_jsonl(tmp_path):
    out = tmp_path / "results.jsonl"
    assert ResultWriter(str(out)).append(make_result()) is True
    lines = _lines(out)
    assert len(lines) == 1
    d = json.loads(lines[0])
    assert d["schema"] == "application-result.v1"
    assert d["result"] == "submitted"


def test_idempotent_on_same_run_and_job(tmp_path):
    out = tmp_path / "results.jsonl"
    w = ResultWriter(str(out))
    assert w.append(make_result()) is True
    assert w.append(make_result()) is False  # same (executor_run_id, integration_id)
    assert len(_lines(out)) == 1


def test_different_run_is_appended(tmp_path):
    out = tmp_path / "results.jsonl"
    w = ResultWriter(str(out))
    w.append(make_result(run_id="run-1"))
    assert w.append(make_result(run_id="run-2")) is True
    assert len(_lines(out)) == 2


def test_different_job_is_appended(tmp_path):
    out = tmp_path / "results.jsonl"
    w = ResultWriter(str(out))
    w.append(make_result(integration_id="linkedin:111"))
    assert w.append(make_result(integration_id="linkedin:222")) is True
    assert len(_lines(out)) == 2


def test_resume_loads_existing_keys(tmp_path):
    out = tmp_path / "results.jsonl"
    ResultWriter(str(out)).append(make_result())
    # A fresh writer over the same file (e.g. resuming the same run) must not
    # re-write a record it already has.
    assert ResultWriter(str(out)).append(make_result()) is False
    assert len(_lines(out)) == 1


def test_failed_result_is_still_written(tmp_path):
    out = tmp_path / "results.jsonl"
    assert ResultWriter(str(out)).append(make_result(result="failed")) is True
    assert json.loads(_lines(out)[0])["result"] == "failed"


def test_invalid_result_raises_and_writes_nothing(tmp_path):
    out = tmp_path / "results.jsonl"
    bad = make_result()
    bad.result = "definitely_applied"  # not a valid result value
    with pytest.raises(SchemaError):
        ResultWriter(str(out)).append(bad)
    assert not out.exists()


def test_append_all_counts_new_writes(tmp_path):
    out = tmp_path / "results.jsonl"
    n = ResultWriter(str(out)).append_all(
        [
            make_result(integration_id="linkedin:1"),
            make_result(integration_id="linkedin:2"),
            make_result(integration_id="linkedin:1"),  # duplicate of the first
        ]
    )
    assert n == 2
    assert len(_lines(out)) == 2


@pytest.mark.asyncio
async def test_results_written_incrementally_through_the_runner(tmp_path):
    out = tmp_path / "results.jsonl"
    writer = ResultWriter(str(out))
    jobs = [make_job(integration_id=f"linkedin:{i}") for i in (1, 2, 3)]

    async def apply_fn(job, config):
        return ApplicationResult.for_job(
            job, executor_run_id="run-1", result="submitted", started_at=NOW, finished_at=NOW
        )

    results = await process_queue(
        jobs,
        apply_fn,
        QueueExecutionConfig(executor_run_id="run-1"),
        on_result=writer.append,
        resume_exists=lambda _p: True,
    )
    assert len(results) == 3
    assert len(_lines(out)) == 3


@pytest.mark.asyncio
async def test_interruption_leaves_durable_records_for_finished_jobs(tmp_path):
    out = tmp_path / "results.jsonl"
    writer = ResultWriter(str(out))
    jobs = [make_job(integration_id="linkedin:1"), make_job(integration_id="linkedin:2")]
    calls = {"n": 0}

    async def pause():
        calls["n"] += 1
        if calls["n"] == 2:  # interrupt before the 2nd job is processed
            raise RuntimeError("interrupted")

    async def apply_fn(job, config):
        return ApplicationResult.for_job(
            job, executor_run_id="run-1", result="submitted", started_at=NOW, finished_at=NOW
        )

    with pytest.raises(RuntimeError):
        await process_queue(
            jobs,
            apply_fn,
            QueueExecutionConfig(executor_run_id="run-1"),
            pause_checker=pause,
            on_result=writer.append,
            resume_exists=lambda _p: True,
        )
    # The first job's result was persisted before the interruption.
    assert len(_lines(out)) == 1
    assert json.loads(_lines(out)[0])["integration_id"] == "linkedin:1"


# --- Phase 8: locking / cross-process durability ---


def test_second_writer_instance_sees_prior_key_and_refuses_dup(tmp_path):
    # Simulates a resumed run (new ResultWriter over an existing file) with the
    # SAME (run, job) key: it must refresh its seen-set under the lock and refuse.
    out = tmp_path / "results.jsonl"
    assert ResultWriter(str(out)).append(make_result()) is True
    # A fresh writer instance (as a second process would have) must not dup it.
    assert ResultWriter(str(out)).append(make_result()) is False
    assert len(_lines(out)) == 1


def test_concurrent_writers_sharing_run_id_write_each_key_once(tmp_path):
    # Two writer instances interleaved over the same file, same run id: each
    # distinct (run, job) lands exactly once, no interleaving/dup.
    out = tmp_path / "results.jsonl"
    w1 = ResultWriter(str(out))
    w2 = ResultWriter(str(out))
    assert w1.append(make_result(integration_id="linkedin:1")) is True
    assert w2.append(make_result(integration_id="linkedin:2")) is True
    assert w2.append(make_result(integration_id="linkedin:1")) is False  # already by w1
    assert w1.append(make_result(integration_id="linkedin:2")) is False  # already by w2
    keys = {(json.loads(l)["executor_run_id"], json.loads(l)["integration_id"]) for l in _lines(out)}
    assert keys == {("run-1", "linkedin:1"), ("run-1", "linkedin:2")}


def test_tolerates_a_torn_trailing_line(tmp_path):
    out = tmp_path / "results.jsonl"
    ResultWriter(str(out)).append(make_result(integration_id="linkedin:1"))
    with open(out, "a", encoding="utf-8") as f:
        f.write('{"partial": ')  # crash mid-write leaves a torn line
    # A new writer ignores the torn line and still appends a new record.
    assert ResultWriter(str(out)).append(make_result(integration_id="linkedin:2")) is True
