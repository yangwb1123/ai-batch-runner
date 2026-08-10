"""Fail-closed health reporting contracts."""

from __future__ import annotations

import subprocess

from pbatch import health


def test_quality_probe_failure_is_unknown_not_clean(tmp_path, monkeypatch):
    monkeypatch.setattr(
        health.subprocess, "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 2, stdout="unexpected output", stderr="broken"))

    assert health._quality_violations(tmp_path) == -1


def test_checker_timeout_is_unknown(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 120)

    monkeypatch.setattr(health.subprocess, "run", timeout)
    assert health._checker_violations("check-design-intelligence", tmp_path) == -1


def test_unknown_probe_keeps_report_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "_count_tests", lambda _root: 1)
    monkeypatch.setattr(health, "_checker_violations", lambda *_args: 0)
    monkeypatch.setattr(health, "_quality_violations", lambda _root: -1)

    report = health.health_report(tmp_path)

    assert report["quality_violations"] == -1
    assert report["healthy"] is False
    assert "技术债(quality): 未知" in health._render(report)
