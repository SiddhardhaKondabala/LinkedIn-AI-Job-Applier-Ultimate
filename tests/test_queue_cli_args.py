"""Phase 3 — queue-mode CLI argument parsing and config construction."""

from src.integration.cli import parse_queue_args, queue_config_from_args


def test_queue_mode_detected_by_approved_jobs():
    assert parse_queue_args(["--approved-jobs", "q.json"]).approved_jobs == "q.json"


def test_not_queue_mode_without_flag():
    assert parse_queue_args([]).approved_jobs is None


def test_config_defaults_are_safe():
    cfg = queue_config_from_args(parse_queue_args(["--approved-jobs", "q.json"]), default_min_score=4.0)
    assert cfg.should_submit is False
    assert cfg.min_score == 4.0
    assert cfg.executor_run_id.startswith("run-")


def test_submit_flag_enables_submission():
    cfg = queue_config_from_args(parse_queue_args(["--approved-jobs", "q.json", "--submit"]))
    assert cfg.should_submit is True


def test_dry_run_blocks_submit():
    cfg = queue_config_from_args(parse_queue_args(["--approved-jobs", "q.json", "--submit", "--dry-run"]))
    assert cfg.dry_run is True
    assert cfg.should_submit is False


def test_explicit_values_round_trip():
    cfg = queue_config_from_args(
        parse_queue_args(
            ["--approved-jobs", "q.json", "--min-score", "4.5", "--max-jobs", "3", "--executor-run-id", "run-x"]
        )
    )
    assert cfg.min_score == 4.5
    assert cfg.max_jobs == 3
    assert cfg.executor_run_id == "run-x"


def test_min_score_defaults_to_queue_value():
    cfg = queue_config_from_args(parse_queue_args(["--approved-jobs", "q.json"]), default_min_score=3.7)
    assert cfg.min_score == 3.7


def test_unknown_args_are_ignored():
    assert parse_queue_args(["--approved-jobs", "q.json", "--whatever", "x"]).approved_jobs == "q.json"
