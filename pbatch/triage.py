"""Failure triage for the 7x24 round loop (R5 / T5).

Three mechanisms keep an unattended run from burning quota against a
broken provider or a poisoned task set:

- Circuit isolation: a task that fails N times in a row (default 5) is
  removed from later rounds — the per-task circuit breaker.
- Stall detection: K consecutive rounds (default 6) with no new successes
  and an unchanged failure set stop the loop (exit 4) instead of spinning
  forever under --max-rounds 0.
- Residue markers: run_task writes a private-runtime
  `state/<key>.start` marker while the agent runs; a marker surviving the
  process (SIGKILL) signals an
  orphaned run, warned about and regenerated next round.
- Environment classification: identical validator stderr across >=2 tasks
  in one round is an environment-class failure (e.g. a broken validator
  binary) that halts the loop rather than retrying fruitlessly.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path
from typing import Optional

from .config import log

def state_dir(cwd: str = "") -> Path:
    """状态标记目录（data_dir 感知：PBATCH_DATA_DIR 可移出项目）。"""
    from .config import data_dir
    return data_dir(cwd) / "state"


_LEGACY_STATE_DIR = Path(".pi-batch") / "state"
STATE_DIR = _LEGACY_STATE_DIR  # 兼容旧引用（新代码用 state_dir()）


def _effective_state_dir(cwd: str = "") -> Path:
    """Resolve marker state under data_dir, retaining explicit legacy overrides."""
    current = Path(STATE_DIR)
    if current != _LEGACY_STATE_DIR:
        return current
    return state_dir(cwd)
CIRCUIT_MAX = 5
STALL_ROUNDS = 6
STALL_EXIT_CODE = 4

_ENV_SIGNATURE_RE = re.compile(r"validator feedback|VALIDATION FAILED", re.IGNORECASE)
_TRANSIENT_RE = re.compile(r"rate|429|quota|network|connect|unreachable|timeout|billing|overloaded", re.IGNORECASE)
_PERMANENT_RE = re.compile(
    r"agent binary not found|not found in PATH|agent adapter error|"
    r"context injection rejected|prompt reference not found|"
    r"output path is a symlink",
    re.IGNORECASE,
)


def task_key(task) -> str:
    """Stable per-task identity for circuit/stall bookkeeping."""
    raw = task.output or task.prompt
    return hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:16]


def marker_path(key: str, cwd: str = "") -> Path:
    return _effective_state_dir(cwd) / f"{key}.start"


def write_marker(key: str, cwd: str = "", agent_pid: Optional[int] = None) -> None:
    """Per-task start marker; a surviving marker after the run signals an
    orphaned (SIGKILLed) agent invocation (zero pi dependency). Content:
    runner pid, agent pid (when known), start epoch — so a residue scan can
    tell whether the orphaned agent is still running (GAP-3/N1)."""
    p = marker_path(key, cwd)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("%s\n%s\n%d\n" % (
            os.getpid(), agent_pid if agent_pid is not None else "",
            int(time.time())), encoding="utf-8")
    except OSError:
        pass  # state dir unwritable: residue detection degrades silently


def clear_marker(key: str, cwd: str = "") -> None:
    try:
        marker_path(key, cwd).unlink(missing_ok=True)
    except OSError:
        pass  # state dir unwritable: marker best-effort


def residue_markers(cwd: str = "") -> list:
    """Markers left by a previous (killed) run; empty when clean."""
    d = _effective_state_dir(cwd)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.start"))


def marker_info(path: Path) -> dict:
    """Parse a start marker into {"runner_pid", "agent_pid", "started"};
    fields absent in old single-line markers are empty/0. Never raises."""
    info = {"runner_pid": 0, "agent_pid": 0, "started": 0}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return info
    if not lines:
        return info
    try:
        info["runner_pid"] = int(lines[0])
    except ValueError:
            pass  # malformed marker line: skip entry
    if len(lines) >= 2 and lines[1].strip():
        try:
            info["agent_pid"] = int(lines[1])
        except ValueError:
            pass  # malformed marker line: skip entry
    if len(lines) >= 3 and lines[2].strip():
        try:
            info["started"] = int(lines[2])
        except ValueError:
            pass  # malformed marker line: skip entry
    return info


def write_cmd_failure_diagnostics(cmd: str, exit_code: int, stderr: str,
                                  cmd_cwd: str) -> None:
    """命令失败 → 有界诊断摘录到用户级 runtime/rejected/（L4 反馈闭环：
    人工/重试可诊断验证实验到底拒绝了什么）。"""
    try:
        from .config import data_dir
        rej = data_dir(cmd_cwd) / "rejected"
        rej.mkdir(parents=True, exist_ok=True)
        name = "verify-cmd-%s-%04d.txt" % (time.strftime("%Y%m%d-%H%M%S"),
                                            abs(hash(cmd)) % 10000)
        body = "$ %s\nexit=%s\n%s" % (cmd, exit_code, (stderr or "")[:65536])
        (rej / name).write_text(body, encoding="utf-8", errors="replace")
        log.info("CMD failure diagnostics: %s", rej / name)
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """True when the pid is a live process in this host's pid namespace."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def kill_orphan(pid: int) -> None:
    """Kill an orphaned agent's whole process group (agents are spawned with
    start_new_session=True so pgid == pid). Best-effort, never raises."""
    if pid <= 0:
        return
    try:
        os.killpg(pid, 9)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass  # process already gone: best-effort kill


def classify_reason(reason: str) -> str:
    """transient (retryable provider/network), permanent (never retryable),
    or environment (surfaced by a gate, not the agent)."""
    if not reason:
        return "transient"
    if _PERMANENT_RE.search(reason):
        return "permanent"
    if "validation failed" in reason or _ENV_SIGNATURE_RE.search(reason):
        return "environment"
    if _TRANSIENT_RE.search(reason):
        return "transient"
    return "transient"


def is_retryable(reason: str) -> bool:
    """Return whether another agent attempt can plausibly change the result.

    Validator failures remain retryable because the previous feedback is
    appended to the next prompt.  Only deterministic runner/configuration
    failures are stopped before spending another invocation.
    """
    return classify_reason(reason) != "permanent"


class Circuit:
    """Per-task failure counter; a task reaching the threshold is isolated
    (removed from later rounds) so a poison task cannot burn the budget."""

    def __init__(self, max_failures: int = CIRCUIT_MAX):
        self.max_failures = max_failures
        self._counts: dict = {}
        self.isolated: set = set()

    def register_failure(self, key: str, reason: str) -> bool:
        """Count one failure; returns True when the task is now isolated."""
        if classify_reason(reason) == "permanent":
            self.isolated.add(key)
            return True
        self._counts[key] = self._counts.get(key, 0) + 1
        if self._counts[key] >= self.max_failures:
            self.isolated.add(key)
            log.warning("CIRCUIT: task %s isolated after %d failures (reason: %s)",
                        key, self._counts[key], reason)
            return True
        return False

    def allowed(self, key: str) -> bool:
        return key not in self.isolated


class StallDetector:
    """K rounds with no new successes and an unchanged failure set stop the
    loop (exit 4) — a stuck environment must not spin forever."""

    def __init__(self, k: int = STALL_ROUNDS):
        self.k = k
        self._prev_failed: frozenset = frozenset()
        self._prev_successes = 0
        self._stagnant = 0

    def update(self, failed_keys: set, new_successes: int) -> bool:
        """Feed one round's outcome; True when the loop must stop."""
        failed = frozenset(failed_keys)
        if failed == self._prev_failed and new_successes <= self._prev_successes:
            self._stagnant += 1
        else:
            self._stagnant = 0
        self._prev_failed = failed
        self._prev_successes = new_successes
        if self._stagnant >= self.k and failed:
            log.error("STALL: %d round(s) with no progress (failure set unchanged); stopping (exit %d)",
                      self._stagnant, STALL_EXIT_CODE)
            return True
        return False


def env_signature(results: list) -> str:
    """Fingerprint of validator failures across a round: identical stderr
    from >=2 tasks is an environment-class failure (halt), not per-task."""
    sigs: dict = {}
    for r in results:
        if not r.validation_stderr:
            continue
        sig = hashlib.sha1(r.validation_stderr.encode("utf-8", errors="replace")).hexdigest()[:12]
        sigs.setdefault(sig, []).append(r)
    for sig, hits in sigs.items():
        if len(hits) >= 2:
            return sig
    return ""
