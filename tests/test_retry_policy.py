"""Regression tests for retry classification and permanent failures."""

from __future__ import annotations

import logging

from pbatch import config, runner, triage
from pbatch import campaign_analysis, meta
from pbatch.models import Stage, Task, TaskResult


def test_retry_classification_keeps_validator_feedback_retryable():
    assert triage.classify_reason("validation failed (exit=1)") == "environment"
    assert triage.is_retryable("validation failed (exit=1)") is True
    assert triage.is_retryable("agent reported provider failure (rate_limit_error)") is True


def test_retry_classification_marks_runner_setup_failures_permanent():
    for reason in (
        "agent binary not found",
        "agent adapter error: prompt flag is invalid",
        "context injection rejected: path escapes root",
        "output path is a symlink (refusing to follow)",
    ):
        assert triage.classify_reason(reason) == "permanent"
        assert triage.is_retryable(reason) is False


def test_missing_agent_skips_retries(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "AGENT_BIN", str(tmp_path / "missing-agent"))
    monkeypatch.setattr(runner, "_scheduled_sleep", lambda _: None)
    task = Task(prompt="should not invoke", output=str(tmp_path / "out.md"),
                cwd=str(tmp_path))
    with caplog.at_level(logging.WARNING):
        result = runner.run_serial([task], retries=3, retry_delay=0)[0]
    assert result.success is False
    assert result.reason == "agent binary not found"
    assert "RETRY SKIPPED" in caplog.text
    assert not (tmp_path / "out.md").exists()


def test_meta_retry_skips_permanent_role_failure(tmp_path, monkeypatch):
    task = Task(prompt="role", output=str(tmp_path / "role.md"), cwd=str(tmp_path))
    result = TaskResult(task=task, success=False, reason="agent binary not found")
    calls = []
    monkeypatch.setattr(meta, "run_task", lambda *args, **kwargs: calls.append(True))
    monkeypatch.setattr(meta, "record_task", lambda *_args, **_kwargs: None)
    still_failed = meta._retry_failed_roles(
        Stage(name="review"), [result], [], [], "", "session", 2)
    assert still_failed == [result]
    assert calls == []


def test_campaign_analysis_retry_skips_permanent_failure(tmp_path, monkeypatch):
    task = Task(prompt="analyze", output=str(tmp_path / "analysis.md"), cwd=str(tmp_path))
    calls = []

    def fake_run_parallel(pending, jobs):
        calls.append((pending, jobs))
        return [TaskResult(task=task, success=False, reason="agent binary not found")]

    monkeypatch.setattr(campaign_analysis, "run_parallel", fake_run_parallel)
    final = campaign_analysis.run_analysis_tasks([task], jobs=1, retries=3, retry_delay=0)
    assert len(calls) == 1
    assert final[str(task.output_path())].reason == "agent binary not found"
