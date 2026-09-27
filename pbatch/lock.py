"""Single-instance lock (R4 / T4, 7x24 governance).

A second pi-batch process must not run against the same worktree: two
round loops would double-spend quota and interleave session writes. The
lock is an O_EXCL-published file `.pi-batch.lock` in the private workspace
runtime (legacy cwd locks are still recognized) holding {pid, start, host,
argv, token} JSON.

Contract (hardened after the round-4 adversarial reviews):

- **Atomic publication**: the payload is written to a unique temp file
  and linked into place (`os.link`), so no reader can ever observe an
  empty/partial lock file (a non-atomic write + separate O_EXCL create
  let a second instance break a lock that was still being born).
- **Stale = dead PID only**: a lock whose holder PID is alive is refused
  (exit 5) regardless of age. Live locks are NEVER broken on age alone —
  the tool's flagship mode is `--max-rounds 0` 7x24 runs that routinely
  exceed 24h, and breaking a live lock displaces a running instance into
  split-brain. Only a lock whose holder is dead (crash/SIGKILL) is broken.
- **Fail closed on unparseable locks**: an unreadable/empty lock file is
  refused (a partial write under the old format is never "free to take").
- **Ownership-checked release**: `release_lock` unlinks the path only
  when the file still carries this process's token — a foreign (successor)
  lock is left in place. Returned Path objects retain the acquisition token,
  so a delayed release cannot act on a newer generation at the same path.
- Every parallel instance publishes its own record. A permanent guard
  serializes reads, reclamation, publication and release across all records.
- **Round re-verification**: `verify_held` lets the round loop confirm
  the lock is still ours between rounds; a lock that vanished (unlinked
  by a concurrent breaker) stops the instance with exit 5 instead of
  continuing in split-brain.
- A Git worktree with any tracked, staged, or non-ignored untracked change
  is never treated as parallel-safe: declared output disjointness cannot
  prove that two agents will not edit the same dirty source. Such a holder
  remains exclusive until the dirty worktree is isolated or the operator
  explicitly handles it.
- `PBATCH_LOCK_FILE` may point to an explicit coordination path; by default
  the lock is already in the private workspace runtime. The guarded workspace
  is still the `cwd`, not the lock-file directory.
- The file is 0o600 and the `-p`/`--prompt` value is redacted from the
  recorded argv (prompt content is sensitive and the file can survive a
  SIGKILL).

Released in finally; skipped for --dry-run; --no-lock is the documented
escape hatch.
"""

from __future__ import annotations

import os
import math
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from .config import data_dir, log
from .lock_storage import (
    MAX_HOLDERS, OwnedLockPath, holder_paths, lock_guard, participant_path,
    publish_holder, read_holder,
)
from .process_exec import run_bounded
from .process_identity import pid_alive

LOCK_NAME = ".pi-batch.lock"
STALE_AGE_SEC = 24 * 3600
LOCK_EXIT_CODE = 5  # new exit-code contract (docs/RUNNING_247.md)

# token -> lock path, per process: release/verify must only ever touch
# locks this process actually created (a successor's file is never ours).
_OWNED: dict = {}
_COORDINATION: dict = {}


def _alive(pid: int) -> bool:
    return pid_alive(pid)


def _redacted_argv(argv: list) -> str:
    """sys.argv for the lock payload with prompt values redacted: the lock
    file is world-listed and can survive a SIGKILL, so `-p SECRET` must not
    land in it. Values following -p/--prompt are replaced; other flags are
    kept verbatim."""
    out = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        out.append(arg)
        if arg in ("-p", "--prompt") and i + 1 < len(argv):
            out.append("[redacted]")
            i += 1
        i += 1
    return " ".join(out)


def _payload(cwd: str, domain: str = "", outputs: tuple = (),
             session_name: str = "") -> dict:
    return {
        "pid": os.getpid(),
        "start": time.time(),
        "host": socket.gethostname(),
        "workspace": str(Path(cwd or os.getcwd()).resolve()),
        "argv": _redacted_argv(sys.argv),
        "token": uuid.uuid4().hex,
        "domain": domain or "default",
        "outputs": list(outputs)[:MAX_DECLARED_OUTPUTS],
        "outputs_complete": len(outputs) <= MAX_DECLARED_OUTPUTS,
        "session_name": session_name or "",
    }


MAX_DECLARED_OUTPUTS = 16
_WORKTREE_PROBE_TIMEOUT = 5


def _worktree_dirty(cwd: str) -> bool:
    """Return a conservative dirty/unknown bit with bounded portable probes."""
    root = str(Path(cwd or os.getcwd()).resolve())
    probe = run_bounded(["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
                        root, _WORKTREE_PROBE_TIMEOUT, 1024, False, "git probe")
    if not probe.ok:
        # Only Git's explicit non-repository result is safe to distinguish
        # from an unavailable command, timeout, or output overflow.
        return "not a git repository" not in probe.stderr.lower()
    return probe.stdout.strip() != "true" or _status_has_output(root)


def _status_has_output(root: str) -> bool:
    """Read both Git pipes with a hard deadline; select(pipe) is not portable."""
    result = run_bounded(
        ["git", "-C", root, "status", "--porcelain=v1", "--untracked-files=normal",
         "--", ".", ":(exclude).pi-batch.lock*"],
        root, _WORKTREE_PROBE_TIMEOUT, 1, False, "git status")
    return not result.ok or bool(result.stdout)


def _default_lock_path(cwd: str = "") -> Path:
    """Return the default private lock, honoring a legacy cwd lock."""
    root = Path(cwd or os.getcwd()).expanduser().resolve()
    legacy = root / LOCK_NAME
    if legacy.exists() or legacy.with_name(legacy.name + ".guard").exists():
        return legacy
    return data_dir(str(root)) / "locks" / LOCK_NAME


def _lock_path(cwd: str) -> Path:
    """Resolve the external coordination file without changing the guarded cwd.

    A pre-existing cwd lock is honored as a migration fallback so an upgrade
    cannot accidentally start beside an older live instance.
    """
    root = Path(cwd or os.getcwd()).expanduser().resolve()
    configured = os.environ.get("PBATCH_LOCK_FILE", "").strip()
    if not configured:
        return _default_lock_path(str(root))
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path(cwd or os.getcwd()) / path
    return path.resolve()


def _prepare_lock(cwd: str, domain: str, outputs: tuple,
                  session_name: str) -> tuple[Path, dict]:
    """Prepare external/runtime coordination state before publication."""
    lock_path = _lock_path(cwd)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.error("Cannot prepare lock directory %s: %s", lock_path.parent, exc)
        raise SystemExit(LOCK_EXIT_CODE) from exc
    payload = _payload(cwd, domain, outputs, session_name)
    payload["worktree_dirty"] = _worktree_dirty(cwd)
    return lock_path, payload


def acquire_lock(cwd: str = "", no_lock: bool = False,
                 wait_minutes: int = 0, domain: str = "",
                 outputs: tuple = (), session_name: str = "") -> Optional[Path]:
    """Acquire a verifiable record for every admitted instance.

    Metadata publication, stale reclamation and release share a permanent
    OS-locked guard. Waiting never holds that guard while sleeping.
    """
    if no_lock:
        return None
    if (type(wait_minutes) not in (int, float) or wait_minutes < 0
            or wait_minutes > 1e9 or not math.isfinite(wait_minutes)):
        raise ValueError("wait_minutes must be finite and non-negative")
    lock_path, payload = _prepare_lock(cwd, domain, outputs, session_name)
    try:
        if wait_minutes > 0:
            return _wait_for_lock(lock_path, wait_minutes, payload, cwd)
        return _attempt_lock(lock_path, payload, cwd)
    except OSError as exc:
        log.error("Cannot coordinate instance lock %s: %s", lock_path, exc)
        raise SystemExit(LOCK_EXIT_CODE) from exc


def _attempt_lock(lock_path: Path, payload: dict, cwd: str,
                  deadline: float | None = None) -> Optional[Path]:
    with lock_guard(lock_path, deadline=deadline):
        for held_path in holder_paths(lock_path):
            if deadline is None and payload["outputs_complete"] and _parallel_or_conflict(
                    held_path, payload["domain"], tuple(payload["outputs"]),
                    payload["session_name"], cwd, payload["worktree_dirty"]):
                continue
            _reject_or_break(held_path, quiet=deadline is not None)
        if len(holder_paths(lock_path)) >= MAX_HOLDERS:
            raise OSError("instance holder count exceeds its limit")
        target = (participant_path(lock_path, payload["token"])
                  if lock_path.exists() else lock_path)
        if not publish_holder(target, payload, staging_dir=lock_path.parent):
            log.error("Instance lock changed during acquisition: %s", target)
            raise SystemExit(LOCK_EXIT_CODE)
        _OWNED[str(target)] = payload["token"]
        _COORDINATION[str(target)] = lock_path
        handle = OwnedLockPath(target)
        handle.owner_token = payload["token"]
        return handle


def _wait_for_lock(lock_path: Path, wait_minutes: float, payload: dict,
                   cwd: str) -> Optional[Path]:
    deadline = time.monotonic() + wait_minutes * 60
    next_message = time.monotonic()
    while True:
        try:
            return _attempt_lock(lock_path, payload, cwd, deadline)
        except SystemExit as exc:
            if exc.code != LOCK_EXIT_CODE:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SystemExit(LOCK_EXIT_CODE)
        if time.monotonic() >= next_message:
            log.info("LOCK: waiting for holder; %.1fs left", remaining)
            next_message = time.monotonic() + 30
        time.sleep(min(0.2, remaining))


def _read_holder(lock_path: Path) -> dict:
    return read_holder(lock_path)


def _local_holder(holder: dict) -> bool:
    return bool(holder) and holder.get("host") == socket.gethostname()


def _parallel_or_conflict(lock_path: Path, domain: str,
                            outputs: tuple, session_name: str,
                            cwd: str = "", worktree_dirty=None) -> bool:
    """Check a request against one of the recorded live participants."""
    holder = _read_holder(lock_path)
    pid = holder.get("pid", 0)
    if not (_local_holder(holder) and _alive(pid)):
        return False  # stale lock: rejected, then broken by _reject_or_break
    if not holder.get("outputs_complete", len(holder.get("outputs", [])) < MAX_DECLARED_OUTPUTS):
        return False
    if _parallel_worktree_blocked(lock_path, holder, cwd, worktree_dirty):
        return False
    holder_domain = str(holder.get("domain", "") or "default")
    request_domain = domain or "default"
    if (holder_domain != "default" and request_domain != "default"
            and holder_domain != request_domain):
        log.warning(
            "Multi-instance mode: holder runs domain '%s', this run "
            "declares domain '%s' — admitting a separate participant.",
            holder_domain, request_domain)
        return True
    return _parallel_declarations(holder, pid, outputs, session_name, cwd)


def _parallel_declarations(holder: dict, pid: int, outputs: tuple,
                           session_name: str, cwd: str = "") -> bool:
    """Apply output/session conflict rules after the worktree safety gate."""
    holder_outputs = _output_paths(holder.get("outputs", []), holder.get("workspace") or cwd)
    request_outputs = _output_paths(outputs, cwd)
    overlap = sorted(str(right) for right in request_outputs
                     if any(left == right or left in right.parents or right in left.parents
                            for left in holder_outputs))
    holder_session = str(holder.get("session_name", "") or "")
    if overlap:
        log.error(
            "Another pi-batch instance is running (pid=%s) and this run "
            "declares OVERLAPPING output path(s): %s. Refusing to start "
            "(exit %d) — use --run-domain or separate output paths.",
            pid, ", ".join(overlap)[:400], LOCK_EXIT_CODE)
        raise SystemExit(LOCK_EXIT_CODE)
    if session_name and holder_session == session_name:
        log.error(
            "Another pi-batch instance is running (pid=%s) with the same "
            "session name '%s' — parallel runs would interleave session "
            "state. Refusing (exit %d); use a different --session-name.",
            pid, session_name, LOCK_EXIT_CODE)
        raise SystemExit(LOCK_EXIT_CODE)
    if not request_outputs and not session_name:
        return False  # unlabelled instance: keep exclusive contract
    log.warning(
        "Multi-instance mode: holder pid=%s declares disjoint outputs — "
        "admitting a separate participant.", pid)
    return True


def _output_paths(outputs, cwd: str) -> set[Path]:
    root = Path(cwd or os.getcwd())
    try:
        return {Path(os.path.normcase(str((root / str(path)).resolve()))) for path in outputs}
    except (ValueError, RuntimeError) as exc:
        raise OSError(f"cannot resolve declared output path: {exc}") from exc


def _parallel_worktree_blocked(lock_path: Path, holder: dict,
                               cwd: str = "", worktree_dirty=None) -> bool:
    """Dirty or unknown Git state vetoes all output-based parallelism."""
    workspace = str(cwd or holder.get("workspace") or lock_path.parent)
    dirty = _worktree_dirty(workspace) if worktree_dirty is None else worktree_dirty
    if not (dirty or holder.get("worktree_dirty") is not False):
        return False
    log.error(
        "Git worktree %s is dirty or its state is unknown; refusing "
        "parallel pi-batch instances (exit %d). Use isolate/worktree "
        "separation instead of sharing a dirty source tree.",
        lock_path.parent, LOCK_EXIT_CODE)
    return True


def _reject_or_break(lock_path: Path, *, quiet: bool = False) -> None:
    """Refuse when the holder is alive or the lock is unparseable (fail
    closed: a lock we cannot read is never free to take); otherwise break
    the stale lock (dead PID only — a live holder is NEVER broken on age
    alone, because 7x24 runs routinely outlive the 24h staleness window)."""
    holder = _read_holder(lock_path)
    pid = holder.get("pid", 0)
    if not _local_holder(holder):
        if not quiet:
            log.error("Lock %s has unreadable or non-local holder metadata; "
                      "refusing to start (exit %d)", lock_path, LOCK_EXIT_CODE)
        raise SystemExit(LOCK_EXIT_CODE)
    if _alive(pid):
        if not quiet:
            log.error("Another pi-batch instance is running (pid=%s, host=%s); "
                      "refusing to start (exit %d)", pid, holder["host"], LOCK_EXIT_CODE)
        raise SystemExit(LOCK_EXIT_CODE)
    log.warning("Breaking stale lock %s (pid=%s dead)", lock_path, pid)
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        raise SystemExit(LOCK_EXIT_CODE)


def verify_held(lock_path: Optional[Path]) -> bool:
    """True while the lock file at *lock_path* is still this process's own
    (token + PID match). A lock that vanished or was replaced by another
    instance means the single-instance guarantee is gone — the round loop
    must stop instead of running in split-brain."""
    if not lock_path:
        return False
    key = str(lock_path)
    token = _OWNED.get(key)
    if not token or getattr(lock_path, "owner_token", token) != token:
        return False
    holder = _read_holder(lock_path)
    return (holder.get("token") == token and holder.get("pid") == os.getpid())


def release_lock(lock_path: Optional[Path]) -> None:
    """Remove the lock file only when it is still ours (best-effort, never
    raises): a foreign lock at the same path must survive — unlinking it
    would let a third instance start while the real holder still runs."""
    if not lock_path:
        return
    key = str(lock_path)
    if key not in _OWNED:
        return
    token = _OWNED[key]
    if getattr(lock_path, "owner_token", token) != token:
        return
    try:
        with lock_guard(_COORDINATION.get(key, lock_path)):
            if _OWNED.get(key) != token:
                return
            if not _read_holder(lock_path) and (lock_path.exists() or lock_path.is_symlink()):
                log.warning("Not removing %s: lock ownership is unreadable", lock_path)
                return
            if verify_held(lock_path):
                lock_path.unlink(missing_ok=True)
            else:
                log.warning("Not removing %s: lock is not owned by this instance", lock_path)
            _OWNED.pop(key, None)
            _COORDINATION.pop(key, None)
    except OSError:
    # best-effort I/O：失败不阻塞主流程（已验证有意）
        pass
