"""Shared Git metadata creation is serialized, not the agents that use it."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
import subprocess
import threading

import pytest

from pbatch import worktree_creation as creation
from pbatch.process_exec import ValidationResult
from test_isolate import repo


def test_common_directory_is_shared_by_linked_worktrees(repo, tmp_path):
    linked = tmp_path / 'linked'
    result = creation.add_worktree(repo, ['-b', 'linked', str(linked), 'HEAD'])
    assert result.returncode == 0, result.stderr
    deadline = creation.time.monotonic() + 5
    assert creation.common_directory(repo, deadline) == creation.common_directory(linked, deadline)
    assert (repo / '.git' / 'abr-worktree-create.guard').is_file()
    assert not (repo / 'abr-worktree-create.guard').exists()


def test_incomplete_commondir_is_not_observed_by_another_creator(repo, tmp_path, monkeypatch):
    """Hold the real Git transaction at the metadata window seen in the CI failure."""
    existing = tmp_path / 'existing'
    assert creation.add_worktree(repo, ['-b', 'existing', str(existing), 'HEAD']).returncode == 0
    commondir = repo / '.git/worktrees/existing/commondir'
    original = commondir.read_bytes()
    entered, waiting, release = threading.Event(), threading.Event(), threading.Event()
    invoke, guard = creation.run_bounded, creation.lock_guard
    calls = []

    def controlled(cmd, cwd, timeout, cap, shell, label):
        if label == 'git worktree add':
            calls.append(cmd)
            if '-b' in cmd and cmd[cmd.index('-b') + 1] == 'first':
                commondir.write_bytes(b'')
                entered.set()
                try:
                    assert release.wait(5)
                finally:
                    commondir.write_bytes(original)
        return invoke(cmd, cwd, timeout, cap, shell, label)

    @contextmanager
    def observed_guard(path, *, deadline):
        if entered.is_set():
            waiting.set()
        with guard(path, deadline=deadline):
            yield

    monkeypatch.setattr(creation, 'run_bounded', controlled)
    monkeypatch.setattr(creation, 'lock_guard', observed_guard)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(creation.add_worktree, repo, ['-b', 'first', str(tmp_path / 'first'), 'HEAD'])
        try:
            assert entered.wait(5)
            second = pool.submit(creation.add_worktree, repo, ['-b', 'second', str(tmp_path / 'second'), 'HEAD'])
            assert waiting.wait(5)
            assert len(calls) == 1  # second must not ask Git to read incomplete metadata
        finally:
            release.set()
        assert first.result(timeout=5).returncode == 0
        assert second.result(timeout=5).returncode == 0
    assert len(calls) == 2 and commondir.read_bytes() == original


@pytest.mark.parametrize('timeout', [True, 0, -1, float('inf'), float('nan'), '30'])
def test_invalid_budget_does_not_probe_or_modify_git(tmp_path, monkeypatch, timeout):
    calls = []
    monkeypatch.setattr(creation, 'run_bounded', lambda *args: calls.append(args))
    with pytest.raises(ValueError):
        creation.add_worktree(tmp_path, ['-b', 'new', 'target'], timeout=timeout)
    assert not calls


def test_probe_lock_and_creation_use_one_deadline(tmp_path, monkeypatch):
    clock, calls = [0.0], []
    monkeypatch.setattr(creation.time, 'monotonic', lambda: clock[0])

    def invoke(cmd, cwd, timeout, cap, shell, label):
        calls.append((label, timeout, cap))
        clock[0] += 2
        return ValidationResult(True, 0, str(tmp_path) + '\n', '', False)

    @contextmanager
    def guard(path, *, deadline):
        assert deadline == 30.0
        clock[0] += 3
        yield

    monkeypatch.setattr(creation, 'run_bounded', invoke)
    monkeypatch.setattr(creation, 'lock_guard', guard)
    assert creation.add_worktree(tmp_path, ['-b', 'test', 'target']).returncode == 0
    assert calls == [('git common directory', 30.0, 8192), ('git worktree add', 25.0, 65536)]


def test_late_lock_acquisition_never_starts_git(tmp_path, monkeypatch):
    clock, calls = [0.0], []
    monkeypatch.setattr(creation.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(creation, 'common_directory', lambda *args: tmp_path)
    monkeypatch.setattr(creation, 'run_bounded', lambda *args: calls.append(args))

    @contextmanager
    def late_guard(path, *, deadline):
        clock[0] = deadline
        yield

    monkeypatch.setattr(creation, 'lock_guard', late_guard)
    result = creation.add_worktree(tmp_path, ['-b', 'test', 'target'])
    assert result.returncode != 0 and 'deadline' in result.stderr
    assert not calls


@pytest.mark.parametrize('outcome', [
    ValidationResult(False, 128, '', 'incomplete metadata', False),
    ValidationResult(False, -1, '', '', True),
])
def test_git_failure_is_preserved_not_retried_or_cleaned(tmp_path, monkeypatch, outcome):
    calls = []
    monkeypatch.setattr(creation, 'common_directory', lambda *args: tmp_path)
    marker = tmp_path / 'partial-metadata'

    def fail(*args):
        marker.write_text('retain')
        calls.append(args)
        return outcome

    monkeypatch.setattr(creation, 'run_bounded', fail)
    result = creation.add_worktree(tmp_path, ['-b', 'test', 'target'])
    assert result.returncode != 0 and len(calls) == 1
    assert marker.read_text() == 'retain'
    if not outcome.timed_out:
        assert result.stderr == 'incomplete metadata'
    # The guard is permanent but the OS lock is released even after failure.
    with creation.lock_guard(tmp_path / 'abr-worktree-create'):
        pass


def test_unsafe_guard_refused_without_running_add(repo, tmp_path):
    target = tmp_path / 'do-not-touch'
    target.write_text('unchanged')
    guard = repo / '.git/abr-worktree-create.guard'
    guard.symlink_to(target)
    result = creation.add_worktree(repo, ['-b', 'unsafe', str(tmp_path / 'unsafe'), 'HEAD'])
    assert result.returncode != 0
    assert target.read_text() == 'unchanged' and guard.is_symlink()
    assert not (tmp_path / 'unsafe').exists()


def test_isolate_and_campaign_share_the_creation_owner(monkeypatch, repo, tmp_path):
    from pbatch import isolate, campaign_worktree
    assert isolate.add_worktree is creation.add_worktree
    assert campaign_worktree.add_worktree is creation.add_worktree
    calls = []
    monkeypatch.setattr(isolate, 'add_worktree', lambda *args: (
        calls.append(args) or subprocess.CompletedProcess([], 128, '', 'creation refused')))
    monkeypatch.setenv('PBATCH_DATA_DIR', str(tmp_path / 'state'))
    assert not isolate._create_worktree(repo, 'bad', tmp_path / 'bad', 'HEAD', None, 'fixture', ())
    assert len(calls) == 1 and not (tmp_path / 'bad').exists()
