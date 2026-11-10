"""Serialize ABR worktree creation across one Git common directory.

Git can enumerate another creator's half-written commondir. Only the metadata
creation command is guarded, not task execution. The permanent cooperative guard
is not a Git index lock, a merge grant, or protection from nonparticipating tools.
Failures retain worktrees/branches; no command retries or automatic cleanup.
"""
from pathlib import Path
import subprocess
import time

from .lock_storage import lock_guard
from .process_exec import run_bounded, validate_timeout


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Git worktree creation deadline exhausted')
    return remaining


def common_directory(root: Path, deadline: float) -> Path:
    result = run_bounded(['git', '-C', str(root), 'rev-parse', '--git-common-dir'],
                         str(root), _remaining(deadline), 8192, False, 'git common directory')
    value = result.stdout.rstrip('\r\n')
    if not result.ok or not value or '\n' in value or '\r' in value or '\0' in value:
        raise OSError('cannot resolve Git common directory')
    path = Path(value)
    common = (path if path.is_absolute() else root / path).resolve(strict=True)
    if not common.is_dir():
        raise OSError('Git common directory is not a directory')
    return common


def add_worktree(root: Path, args: list[str], *, timeout: float = 30.0):
    """Probe, acquire and create once within a shared finite admission budget."""
    validate_timeout(timeout, 'Git worktree creation')
    if timeout <= 0:
        raise ValueError('Git worktree creation timeout must be positive')
    deadline = time.monotonic() + timeout
    command = ['git', '-C', str(root), 'worktree', 'add', *args]
    try:
        common = common_directory(root, deadline)
        with lock_guard(common / 'abr-worktree-create', deadline=deadline):
            result = run_bounded(command, str(root), _remaining(deadline), 65536,
                                 False, 'git worktree add')
            # A late result is not accepted even if the subprocess reports exit 0.
            _remaining(deadline)
            stderr = result.stderr or ('Git worktree creation timed out' if result.timed_out else '')
            return subprocess.CompletedProcess(command, result.exit_code, result.stdout, stderr)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 2, '', str(exc))
