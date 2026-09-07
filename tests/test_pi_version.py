"""Contracts for bounded, fail-closed canonical Pi version discovery."""

from __future__ import annotations

import io
import os
from pathlib import Path
import signal
import subprocess

import pytest

from pbatch.pi_version import (
    PiStableVersion,
    parse_pi_stable_version,
    probe_pi_version,
    resolve_pi_executable,
    select_pi_executable_token,
)


class _FakeProcess:
    def __init__(self, stdout=b"0.84.3\n", stderr=b"", returncode=0,
                 *, times_out=False):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.pid = None
        self.times_out = times_out
        self.killed = False
        self.wait_timeouts = []

    def wait(self, timeout=None):
        self.wait_timeouts.append(timeout)
        if self.times_out and not self.killed:
            raise subprocess.TimeoutExpired(("pi", "--version"), timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


def _executable(tmp_path: Path, name: str = "pi") -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(("text", "release"), (
    ("0.84.3\n", (0, 84, 3)),
    ("0.84.10\r\n", (0, 84, 10)),
    (b"1.0.0", (1, 0, 0)),
    ("0.84.3+linux.x86-64\n", (0, 84, 3)),
))
def test_strict_stable_semver_parser_accepts_canonical_versions(text, release):
    version = parse_pi_stable_version(text)
    assert isinstance(version, PiStableVersion)
    assert version.release == release


@pytest.mark.parametrize("text", (
    "", "\n", "v0.84.3\n", "pi 0.84.3\n", "0.84.3-rc.1\n",
    "00.84.3\n", "0.084.3\n", "0.84.03\n", "0.84\n",
    "0.84.3\nextra\n", " 0.84.3\n", "0.84.3 \n",
    "\x1b[32m0.84.3\x1b[0m\n", b"\xff\n",
))
def test_strict_stable_semver_parser_rejects_ambiguous_output(text):
    with pytest.raises(ValueError):
        parse_pi_stable_version(text)


def test_executable_resolution_uses_one_token_and_target_cwd(tmp_path):
    binary = _executable(tmp_path, "pi with spaces")
    resolved = resolve_pi_executable("./pi with spaces", cwd=tmp_path)
    assert resolved == str(binary.resolve())

    with pytest.raises(FileNotFoundError):
        resolve_pi_executable("pi -ne", cwd=tmp_path, search_path=str(tmp_path))


def test_executable_selection_precedence_preserves_one_path_token():
    environment = {"PBATCH_PI_BIN": "/path with spaces/pi-edge"}
    assert select_pi_executable_token(
        "/explicit/pi", fallback="pi", environ=environment) == "/explicit/pi"
    assert select_pi_executable_token(
        fallback="pi", environ=environment) == "/path with spaces/pi-edge"
    assert select_pi_executable_token(
        fallback="/configured/pi", environ={}) == "/configured/pi"


def test_probe_reuses_exact_resolved_executable_and_spawn_once(tmp_path):
    binary = _executable(tmp_path)
    calls = []

    def spawn(argv, cwd):
        calls.append((tuple(argv), cwd))
        return _FakeProcess()

    result = probe_pi_version(
        "./pi", cwd=tmp_path, spawn=spawn, timeout=1.0)

    assert calls == [((str(binary.resolve()), "--version"), str(tmp_path.resolve()))]
    assert result.executable == str(binary.resolve())
    assert result.reported_version == "0.84.3"
    assert result.session_scoped_settings is True
    assert result.supports_session_scoped_settings is True


@pytest.mark.parametrize(("version", "supported"), (
    (b"0.84.2\n", False),
    (b"0.84.3\n", True),
    (b"0.84.3+vendor\n", True),
    (b"0.84.10\n", True),
    (b"1.0.0\n", True),
))
def test_session_settings_gate_uses_numeric_stable_threshold(
        tmp_path, version, supported):
    binary = _executable(tmp_path)
    result = probe_pi_version(
        binary, cwd=tmp_path, spawn=lambda _argv, _cwd: _FakeProcess(version))
    assert result.session_scoped_settings is supported


@pytest.mark.parametrize(("process", "status"), (
    (_FakeProcess(stdout=b"0.84.3\n", stderr=b"warning\n"), "invalid_output"),
    (_FakeProcess(stdout=b"0.84.3\n", returncode=2), "failed"),
    (_FakeProcess(stdout=b"0.84.3-rc.1\n"), "invalid_output"),
    (_FakeProcess(stdout=b"x" * 5000), "output_overflow"),
))
def test_probe_failures_never_enable_session_settings(tmp_path, process, status):
    binary = _executable(tmp_path)
    result = probe_pi_version(
        binary, cwd=tmp_path, output_max_bytes=64,
        spawn=lambda _argv, _cwd: process)
    assert result.status == status
    assert result.session_scoped_settings is False
    assert result.reported_version == ""


def test_timeout_kills_process_and_fails_closed(tmp_path):
    binary = _executable(tmp_path)
    process = _FakeProcess(times_out=True)
    result = probe_pi_version(
        binary, cwd=tmp_path, timeout=0.01,
        spawn=lambda _argv, _cwd: process)
    assert result.status == "timeout"
    assert result.session_scoped_settings is False
    assert process.killed is True
    assert process.wait_timeouts[0] <= 0.01


def test_timeout_targets_the_spawned_process_group(monkeypatch, tmp_path):
    binary = _executable(tmp_path)
    process = _FakeProcess(times_out=True)
    process.pid = 4242
    calls = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))

    result = probe_pi_version(
        binary, cwd=tmp_path, timeout=0.01,
        spawn=lambda _argv, _cwd: process)

    assert result.status == "timeout"
    assert calls == [(4242, signal.SIGKILL)]


def test_spawn_failure_is_bounded_and_fail_closed(tmp_path):
    binary = _executable(tmp_path)

    def fail(_argv, _cwd):
        raise OSError("x" * 10_000)

    result = probe_pi_version(binary, cwd=tmp_path, spawn=fail)
    assert result.status == "spawn_failed"
    assert result.session_scoped_settings is False
    assert len(result.reason.encode("utf-8")) <= 512


def test_default_spawn_is_pipe_bounded_and_noninteractive(monkeypatch, tmp_path):
    binary = _executable(tmp_path)
    calls = []

    def popen(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return _FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = probe_pi_version(binary, cwd=tmp_path)
    assert result.session_scoped_settings is True
    kwargs = calls[0][1]
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.PIPE
    assert kwargs["start_new_session"] is True


@pytest.mark.parametrize(("timeout", "maximum"), (
    (0, 64), (float("nan"), 64), (1, 0), (1, True),
))
def test_probe_boundaries_are_validated_before_spawn(
        tmp_path, timeout, maximum):
    binary = _executable(tmp_path)
    called = []
    with pytest.raises(ValueError):
        probe_pi_version(
            binary, cwd=tmp_path, timeout=timeout, output_max_bytes=maximum,
            spawn=lambda *_args: called.append(True))
    assert called == []
