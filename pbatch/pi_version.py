"""Fail-closed version discovery for the canonical Pi executable.

Pi's RPC handshake does not expose a runtime version.  This module therefore
probes the exact executable token that a caller can subsequently reuse for
RPC startup.  Probe output is deliberately small and strict: an unfamiliar
version string disables version-gated features instead of guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Optional, Sequence
from .provider_env import provider_child_env


MIN_SESSION_SETTINGS_VERSION = (0, 84, 3)
DEFAULT_PROBE_TIMEOUT = 5.0
DEFAULT_OUTPUT_MAX_BYTES = 4096
MAX_EXECUTABLE_BYTES = 4096
MAX_VERSION_BYTES = 128
_VERSION_RE = re.compile(
    r"(0|[1-9][0-9]{0,8})\."
    r"(0|[1-9][0-9]{0,8})\."
    r"(0|[1-9][0-9]{0,8})"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
Spawn = Callable[[Sequence[str], str], Any]
PI_BINARY_ENV = "PBATCH_PI_BIN"


@dataclass(frozen=True)
class PiStableVersion:
    """A canonical stable SemVer reported by Pi's ``--version`` flag."""

    major: int
    minor: int
    patch: int
    raw: str

    @property
    def release(self) -> tuple[int, int, int]:
        return self.major, self.minor, self.patch


@dataclass(frozen=True)
class PiVersionProbe:
    """Bounded observation tied to the exact executable that was probed."""

    executable: str
    status: str
    version: Optional[PiStableVersion] = None
    reason: str = ""

    @property
    def reported_version(self) -> str:
        return self.version.raw if self.version is not None else ""

    @property
    def session_scoped_settings(self) -> bool:
        return (self.status == "observed" and self.version is not None
                and self.version.release >= MIN_SESSION_SETTINGS_VERSION)

    @property
    def supports_session_scoped_settings(self) -> bool:
        """Explicit capability spelling for adapter integrations."""
        return self.session_scoped_settings


@dataclass
class _Capture:
    maximum: int
    data: bytearray = field(default_factory=bytearray)
    overflow: bool = False
    error: str = ""

    def add(self, chunk: bytes) -> None:
        remaining = max(0, self.maximum - len(self.data))
        self.data.extend(chunk[:remaining])
        if len(chunk) > remaining:
            self.overflow = True


@dataclass(frozen=True)
class _ProcessOutput:
    returncode: int
    stdout: _Capture
    stderr: _Capture
    timed_out: bool = False


def parse_pi_stable_version(value: str | bytes) -> PiStableVersion:
    """Parse one strict stable SemVer line; prereleases and prefixes fail."""
    raw = _version_bytes(value)
    if len(raw) > MAX_VERSION_BYTES:
        raise ValueError("Pi version output is too long")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Pi version output is not UTF-8") from exc
    body = _remove_one_line_ending(text)
    match = _VERSION_RE.fullmatch(body)
    if match is None:
        raise ValueError("Pi version is not a stable canonical SemVer")
    return PiStableVersion(
        int(match.group(1)), int(match.group(2)), int(match.group(3)), body)


def _version_bytes(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        return value
    if not isinstance(value, str):
        raise TypeError("Pi version output must be str or bytes")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Pi version output is not UTF-8") from exc


def _remove_one_line_ending(text: str) -> str:
    if text.endswith("\r\n"):
        body = text[:-2]
    elif text.endswith("\n"):
        body = text[:-1]
    else:
        body = text
    if not body or "\n" in body or "\r" in body:
        raise ValueError("Pi version output must contain exactly one line")
    return body


def resolve_pi_executable(
        agent: str | os.PathLike[str] = "pi", *, cwd: str | os.PathLike[str] = "",
        search_path: Optional[str] = None) -> str:
    """Resolve one executable token without shell or ``shlex`` semantics."""
    workdir = _resolved_cwd(cwd)
    token = _executable_token(agent)
    candidate = _cwd_relative_candidate(token, workdir)
    resolved = shutil.which(candidate, path=search_path)
    if resolved is None:
        raise FileNotFoundError(f"Pi executable was not found: {token!r}")
    return os.path.realpath(os.path.abspath(resolved))


def select_pi_executable_token(
        explicit: str | os.PathLike[str] = "", *, fallback: str = "pi",
        environ: Optional[dict[str, str]] = None) -> str:
    """Select one Pi executable token with explicit > environment > fallback."""
    if explicit:
        return _executable_token(explicit)
    values = os.environ if environ is None else environ
    override = str(values.get(PI_BINARY_ENV, "") or "").strip()
    return _executable_token(override or fallback or "pi")


def _resolved_cwd(cwd: str | os.PathLike[str]) -> str:
    path = Path(cwd or os.getcwd()).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"Pi probe cwd is not a directory: {path}")
    return str(path)


def _executable_token(agent: str | os.PathLike[str]) -> str:
    if not isinstance(agent, (str, os.PathLike)):
        raise TypeError("Pi executable must be a string or path")
    token = os.fsdecode(agent)
    if not token or "\x00" in token:
        raise ValueError("Pi executable must be one non-empty safe token")
    if len(token.encode("utf-8")) > MAX_EXECUTABLE_BYTES:
        raise ValueError("Pi executable path is too long")
    return token


def _cwd_relative_candidate(token: str, cwd: str) -> str:
    expanded = os.path.expanduser(token)
    has_separator = os.sep in expanded or bool(
        os.altsep and os.altsep in expanded)
    if has_separator and not os.path.isabs(expanded):
        return os.path.join(cwd, expanded)
    return expanded


def probe_pi_version(
        agent: str | os.PathLike[str] = "pi", *, cwd: str | os.PathLike[str] = "",
        search_path: Optional[str] = None, timeout: float = DEFAULT_PROBE_TIMEOUT,
        output_max_bytes: int = DEFAULT_OUTPUT_MAX_BYTES,
        spawn: Optional[Spawn] = None) -> PiVersionProbe:
    """Probe Pi once and return a fail-closed observation of its version."""
    timeout = _positive_timeout(timeout)
    maximum = _positive_maximum(output_max_bytes)
    workdir = _resolved_cwd(cwd)
    executable = resolve_pi_executable(
        agent, cwd=workdir, search_path=search_path)
    argv = (executable, "--version")
    try:
        process = (spawn or _default_spawn)(argv, workdir)
    except Exception as exc:
        return _unknown(executable, "spawn_failed", exc)
    output = _collect_process(process, argv, timeout, maximum)
    return _interpret_output(executable, output)


def _positive_timeout(value: float) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError("Pi version timeout must be a positive finite number")
    return float(value)


def _positive_maximum(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("Pi version output_max_bytes must be a positive integer")
    return value


def _default_spawn(argv: Sequence[str], cwd: str) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True, env=provider_child_env())


def _collect_process(
        process: Any, argv: Sequence[str], timeout: float,
        maximum: int) -> _ProcessOutput:
    stdout, stderr = _process_captures(process, maximum)
    if stdout is None or stderr is None:
        _kill_process_group(process)
        missing = _Capture(maximum, error="Pi version process pipes are unavailable")
        return _ProcessOutput(-1, stdout or missing, stderr or missing)
    threads = _start_drainers(process, stdout, stderr)
    deadline = time.monotonic() + timeout
    returncode, timed_out = _wait_until(process, argv, deadline)
    if timed_out:
        _kill_process_group(process)
    drain_timed_out = not _join_until(threads, deadline)
    if drain_timed_out:
        _kill_process_group(process)
        _close_process_pipes(process)
        _join_cleanup(threads)
    return _ProcessOutput(
        returncode, stdout, stderr, timed_out or drain_timed_out)


def _process_captures(
        process: Any, maximum: int) -> tuple[Optional[_Capture], Optional[_Capture]]:
    stdout = _Capture(maximum) if getattr(process, "stdout", None) is not None else None
    stderr = _Capture(maximum) if getattr(process, "stderr", None) is not None else None
    return stdout, stderr


def _start_drainers(
        process: Any, stdout: _Capture,
        stderr: _Capture) -> tuple[threading.Thread, threading.Thread]:
    threads = (
        threading.Thread(
            target=_drain_stream, args=(process.stdout, stdout), daemon=True),
        threading.Thread(
            target=_drain_stream, args=(process.stderr, stderr), daemon=True),
    )
    for thread in threads:
        thread.start()
    return threads


def _drain_stream(stream: Any, capture: _Capture) -> None:
    try:
        while True:
            value = stream.read(4096)
            if not value:
                return
            chunk = value.encode("utf-8", "replace") if isinstance(value, str) else bytes(value)
            capture.add(chunk)
    except Exception as exc:
        capture.error = _bounded_reason(exc)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _wait_until(
        process: Any, argv: Sequence[str],
        deadline: float) -> tuple[int, bool]:
    remaining = max(0.0, deadline - time.monotonic())
    try:
        result = process.wait(timeout=remaining)
    except (subprocess.TimeoutExpired, TimeoutError):
        return -1, True
    try:
        return int(result), False
    except (TypeError, ValueError):
        code = getattr(process, "returncode", None)
        return (int(code), False) if isinstance(code, int) else (-1, False)


def _join_until(threads: Sequence[threading.Thread], deadline: float) -> bool:
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    return not any(thread.is_alive() for thread in threads)


def _join_cleanup(threads: Sequence[threading.Thread]) -> None:
    cleanup_deadline = time.monotonic() + 0.25
    for thread in threads:
        thread.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))


def _kill_process_group(process: Any) -> None:
    pid = getattr(process, "pid", None)
    killed = False
    if isinstance(pid, int) and pid > 0 and hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            killed = True
        except (OSError, ProcessLookupError):
            pass
    if not killed:
        try:
            process.kill()
        except Exception:
            pass
    try:
        process.wait(timeout=0.25)
    except Exception:
        pass


def _close_process_pipes(process: Any) -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(process, name, None)
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass


def _interpret_output(executable: str, output: _ProcessOutput) -> PiVersionProbe:
    if output.timed_out:
        return _unknown(executable, "timeout", "Pi --version exceeded its deadline")
    if output.stdout.error or output.stderr.error:
        return _unknown(executable, "read_failed", "Pi --version pipe read failed")
    if output.stdout.overflow or output.stderr.overflow:
        return _unknown(executable, "output_overflow", "Pi --version output exceeded its boundary")
    if output.returncode != 0:
        return _unknown(executable, "failed", f"Pi --version exited with code {output.returncode}")
    if output.stderr.data:
        return _unknown(executable, "invalid_output", "Pi --version wrote to stderr")
    try:
        version = parse_pi_stable_version(bytes(output.stdout.data))
    except (TypeError, ValueError) as exc:
        return _unknown(executable, "invalid_output", exc)
    return PiVersionProbe(executable, "observed", version)


def _unknown(executable: str, status: str, reason: Any) -> PiVersionProbe:
    return PiVersionProbe(executable, status, reason=_bounded_reason(reason))


def _bounded_reason(value: Any, maximum: int = 512) -> str:
    text = " ".join(str(value if value is not None else "").split())
    encoded = text.encode("utf-8", "replace")
    return encoded[:maximum].decode("utf-8", "ignore")


__all__ = [
    "DEFAULT_OUTPUT_MAX_BYTES", "DEFAULT_PROBE_TIMEOUT",
    "MIN_SESSION_SETTINGS_VERSION", "PiStableVersion", "PiVersionProbe",
    "parse_pi_stable_version", "probe_pi_version", "resolve_pi_executable",
]
