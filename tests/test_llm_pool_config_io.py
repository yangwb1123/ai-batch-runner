"""Configuration management boundaries, failure atomicity and cooperating writers."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import multiprocessing
import os
from pathlib import Path
import stat
import time
import traceback

import pytest
import yaml

from pbatch import llm_pool_config_io as io

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX management primitive")


@pytest.fixture
def pool(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    tmp_path.chmod(0o755)
    data = {"schema_version": 1, "pool_id": "operations",
            "models": {"model-a": "chat/completions"},
            "gateway": {"token_file": "keys/token"},
            "accounts": [{"id": "a", "quota_group": "a", "credential_file": "keys/a",
                          "priority": 0}]}
    path = tmp_path / "pool.yaml"
    path.write_bytes(b"# preserve this comment\n" + yaml.safe_dump(data).encode())
    path.chmod(0o644)
    return path, data, path.read_bytes()


def _bump(data):
    data["accounts"][0]["priority"] += 1
    return data


def _artifacts(path):
    return sorted(p.name for p in path.parent.glob(f".{path.name}.*")
                  if p.name != f".{path.name}.lock")


def test_read_update_backup_and_noop(pool):
    path, data, raw = pool
    digest = hashlib.sha256(raw).hexdigest()
    assert io.read_document(path) == (data, raw, digest)
    report = io.edit_document(path, _bump, expected_revision=digest)
    assert set(report) == {"changed", "config_file", "backup_file", "revision",
                           "result_revision", "restart_required"}
    assert report["changed"] and report["restart_required"]
    assert report["config_file"] == str(path)
    assert report["revision"] == digest
    assert report["result_revision"] == hashlib.sha256(path.read_bytes()).hexdigest()
    backup = Path(report["backup_file"])
    assert backup.read_bytes() == raw
    lock = path.with_name(".pool.yaml.lock")
    for item in (path, backup, lock):
        assert stat.S_IMODE(item.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    identity = path.stat().st_ino, lock.stat().st_ino
    noop = io.edit_document(path, lambda d: d)
    assert not noop["changed"] and not noop["restart_required"]
    assert noop["backup_file"] is None
    assert noop["result_revision"] == noop["revision"] == report["result_revision"]
    assert (path.stat().st_ino, lock.stat().st_ino) == identity
    second = io.edit_document(path, _bump)
    assert second["backup_file"] != report["backup_file"]
    assert backup.read_bytes() == raw


@pytest.mark.parametrize("create", [False, True])
def test_dry_run_does_not_write_anything_or_read_secrets(pool, monkeypatch, create):
    from pbatch import llm_pool_secrets
    path, data, raw = pool
    if create:
        path = path.parent / "missing" / "nested" / "pool.yaml"
    real_open = os.open
    def open_readonly(name, flags, *args, **kwargs):
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        return real_open(name, flags, *args, **kwargs)
    def forbidden(*args, **kwargs):
        pytest.fail("dry run attempted a write or secret access")
    monkeypatch.setattr(os, "open", open_readonly)
    for name in ("mkdir", "replace", "link", "unlink", "fsync", "chmod", "fchmod"):
        monkeypatch.setattr(os, name, forbidden)
    for name in ("read_secret", "write_secret"):
        monkeypatch.setattr(llm_pool_secrets, name, forbidden)
    def transform(before):
        assert before is None if create else before == data
        return _bump(copy.deepcopy(data))
    report = io.edit_document(path, transform, create=create, dry_run=True)
    assert report["backup_file"] is None and report["changed"]
    assert report["revision"] == (None if create else hashlib.sha256(raw).hexdigest())
    assert len(report["result_revision"]) == 64 and report["restart_required"]
    assert not (pool[0].parent / "missing").exists()
    assert sorted(p.name for p in pool[0].parent.iterdir()) == ["pool.yaml"]
    assert pool[0].read_bytes() == raw


def test_init_creates_private_parents_and_never_overwrites(pool):
    path, data, _ = pool
    new = path.parent / "new" / "nested" / "new.yaml"
    def transform(before):
        assert before is None
        return data
    report = io.edit_document(new, transform, create=True)
    assert report["revision"] is None and report["backup_file"] is None
    assert io.read_document(new)[0] == data
    assert new.stat().st_nlink == 1
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    for directory in (new.parent, new.parent.parent):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    original = new.read_bytes()
    with pytest.raises(FileExistsError):
        io.edit_document(new, transform, create=True)
    assert new.read_bytes() == original and not _artifacts(new)


@pytest.mark.parametrize("dry_run", [False, True])
def test_missing_existing_and_unchanged_modes(pool, dry_run):
    path, _, raw = pool
    with pytest.raises(FileNotFoundError):
        io.edit_document(path.with_name("absent"), _bump, dry_run=dry_run)
    with pytest.raises(FileExistsError):
        io.edit_document(path, _bump, create=True, dry_run=dry_run)
    report = io.edit_document(path, lambda d: d, dry_run=dry_run)
    assert not report["changed"] and path.read_bytes() == raw
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert not _artifacts(path)


@pytest.mark.parametrize("revision", ["", "A" * 64, "g" * 64, "a" * 63, "a" * 65, 1])
def test_invalid_expected_revision_does_not_write(pool, revision):
    path, _, raw = pool
    with pytest.raises(ValueError):
        io.edit_document(path, _bump, expected_revision=revision)
    assert path.read_bytes() == raw
    assert list(path.parent.iterdir()) == [path]


def test_preview_revision_rejects_stale_apply_and_init_revision(pool):
    path, data, raw = pool
    preview = io.edit_document(path, _bump, dry_run=True)
    path.write_bytes(raw + b"# external edit\n")
    with pytest.raises(io.ConfigConflictError):
        io.edit_document(path, _bump, expected_revision=preview["revision"])
    assert path.read_bytes() == raw + b"# external edit\n"
    with pytest.raises(ValueError):
        io.edit_document(path.parent / "new", lambda _: data, create=True,
                         expected_revision=preview["revision"])
    assert not _artifacts(path)


@pytest.mark.parametrize("raw", [b"accounts: [sensitive-marker", b"schema_version: 1\nschema_version: 1",
                               b"x: &a [*a]", b"\xff", b"x" * 65537, b"{}", b"[]"])
def test_invalid_input_is_sanitized_and_preserved(pool, raw):
    path, _, _ = pool
    path.write_bytes(raw)
    with pytest.raises(ValueError) as caught:
        io.edit_document(path, _bump)
    assert "sensitive-marker" not in "".join(traceback.format_exception(caught.value))
    assert path.read_bytes() == raw and not _artifacts(path)


@pytest.mark.parametrize("dry_run", [False, True])
def test_safe_command_error_survives_without_rewriting(pool, dry_run):
    path, _, raw = pool
    error = io.PoolConfigCommandError("unknown account; list accounts first")
    inode = path.stat().st_ino
    def transform(data):
        _bump(data)
        raise error
    with pytest.raises(io.PoolConfigCommandError) as caught:
        io.edit_document(path, transform, dry_run=dry_run)
    assert caught.value is error and path.stat().st_ino == inode
    assert path.read_bytes() == raw and not _artifacts(path)


@pytest.mark.parametrize("kind", ["field", "unknown", "empty", "oversized", "exception"])
def test_invalid_update_is_sanitized_and_preserved(pool, kind):
    path, _, raw = pool
    def transform(data):
        if kind == "exception":
            raise ValueError("sensitive-marker")
        if kind == "field":
            data["accounts"][0]["priority"] = "sensitive-marker"
        elif kind == "unknown":
            data["sensitive-marker"] = True
        elif kind == "empty":
            data["accounts"] = []
        else:
            data["gateway"]["token_file"] = "x" * 65537
        return data
    with pytest.raises(ValueError) as caught:
        io.edit_document(path, transform)
    assert "sensitive-marker" not in "".join(traceback.format_exception(caught.value))
    assert path.read_bytes() == raw and not _artifacts(path)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory", "writable", "owner"])
@pytest.mark.parametrize("target", ["config", "lock"])
def test_rejects_unsafe_files_without_blocking(pool, monkeypatch, kind, target):
    path, _, raw = pool
    item = path if target == "config" else path.with_name(".pool.yaml.lock")
    if item.exists():
        item.unlink()
    other = path.parent / "other"
    other.write_bytes(raw)
    if kind == "symlink":
        item.symlink_to(other)
    elif kind == "hardlink":
        item.hardlink_to(other)
    elif kind == "fifo":
        os.mkfifo(item, 0o600)
    elif kind == "directory":
        item.mkdir()
    else:
        item.write_bytes(raw)
        item.chmod(0o666 if kind == "writable" else 0o600)
    if kind == "owner":
        real_fstat, inode = os.fstat, item.stat().st_ino
        def fstat(descriptor):
            info = real_fstat(descriptor)
            if info.st_ino == inode:
                fields = list(info)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return info
        monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises((OSError, ValueError)):
        io.edit_document(path, _bump)
    assert other.read_bytes() == raw and not _artifacts(path)


@pytest.mark.parametrize("mode", [0o640, 0o644, 0o660])
def test_existing_lock_must_be_exactly_private(pool, mode):
    path, _, raw = pool
    lock = path.with_name(".pool.yaml.lock")
    lock.write_bytes(b"do not truncate")
    lock.chmod(mode)
    with pytest.raises(ValueError):
        io.edit_document(path, _bump)
    assert lock.read_bytes() == b"do not truncate" and path.read_bytes() == raw


@pytest.mark.parametrize("mode", [0o775, 0o777])
def test_rejects_writable_parent_without_chmod(pool, mode):
    path, _, raw = pool
    path.parent.chmod(mode)
    with pytest.raises(ValueError):
        io.edit_document(path, _bump)
    assert stat.S_IMODE(path.parent.stat().st_mode) == mode
    assert path.read_bytes() == raw and list(path.parent.iterdir()) == [path]


def test_rejects_symlink_ancestor_and_dotdot(pool):
    path, _, raw = pool
    alias = path.parent / "alias"
    alias.symlink_to(path.parent, target_is_directory=True)
    for candidate in (alias / path.name, alias / "missing" / path.name,
                      alias / ".." / path.parent.name / path.name):
        with pytest.raises((OSError, ValueError)):
            io.edit_document(candidate, _bump, create=True, dry_run=True)
        with pytest.raises((OSError, ValueError)):
            io.read_document(candidate)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("reference", ["pool.yaml", ".pool.yaml.lock", ".pool.yaml.backup-any",
                                     ".pool.yaml.temp-any", ".pool.yaml.other",
                                     ".pool.yaml.lock/secret"])
@pytest.mark.parametrize("field", ["token", "credential"])
@pytest.mark.parametrize("before", [False, True])
def test_credentials_cannot_overlap_management_namespace(pool, reference, field, before):
    path, data, raw = pool
    def transform(value):
        if field == "token":
            value["gateway"]["token_file"] = reference
        else:
            value["accounts"][0]["credential_file"] = reference
        return value
    if before:
        path.write_text(yaml.safe_dump(transform(data)))
        raw = path.read_bytes()
    with pytest.raises(ValueError):
        io.edit_document(path, (lambda d: d) if before else transform)
    assert path.read_bytes() == raw and not _artifacts(path)


@pytest.mark.parametrize("method", ["inplace", "replace", "delete", "late"])
def test_external_edit_conflicts_preserve_external_bytes(pool, monkeypatch, method):
    path, _, raw = pool
    external = raw.replace(b"priority: 0", b"priority: 9")
    def modify():
        if method == "delete":
            path.unlink()
        elif method == "replace":
            other = path.with_name("external")
            other.write_bytes(external)
            other.replace(path)
        else:
            times = path.stat()
            path.write_bytes(external)
            os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))
    def transform(data):
        if method != "late":
            modify()
        return _bump(data)
    real_write = io._write_file
    def write_file(directory, name, kind, contents):
        artifact = real_write(directory, name, kind, contents)
        if kind == "temp":
            modify()
        return artifact
    if method == "late":
        monkeypatch.setattr(io, "_write_file", write_file)
    with pytest.raises(io.ConfigConflictError):
        io.edit_document(path, transform)
    assert not path.exists() if method == "delete" else path.read_bytes() == external
    assert not _artifacts(path)


def test_read_checks_fstat_after_bounded_read(pool, monkeypatch):
    path, _, raw = pool
    real_fstat, inode, calls = os.fstat, path.stat().st_ino, []
    def fstat(descriptor):
        info = real_fstat(descriptor)
        if info.st_ino == inode:
            calls.append(1)
            if len(calls) == 2:
                path.write_bytes(raw + b"# external\n")
                return real_fstat(descriptor)
        return info
    monkeypatch.setattr(os, "fstat", fstat)
    with pytest.raises(io.ConfigConflictError):
        io.read_document(path)


@pytest.mark.parametrize("failure", [1, 2, 3, "replace"])
@pytest.mark.parametrize("cancel", [False, True])
def test_precommit_failure_cleans_only_own_files(pool, monkeypatch, failure, cancel):
    path, _, raw = pool
    foreign = path.with_name(".pool.yaml.temp-foreign")
    foreign.write_bytes(b"someone else's file")
    real_fsync, calls = os.fsync, []
    def fail(*args, **kwargs):
        raise KeyboardInterrupt() if cancel else OSError("injected failure")
    def fsync(descriptor):
        calls.append(1)
        if len(calls) == failure:
            fail()
        return real_fsync(descriptor)
    monkeypatch.setattr(os, "fsync", fsync)
    if failure == "replace":
        monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(KeyboardInterrupt if cancel else OSError):
        io.edit_document(path, _bump)
    assert path.read_bytes() == raw
    assert _artifacts(path) == [foreign.name]
    assert foreign.read_bytes() == b"someone else's file"
    assert path.with_name(".pool.yaml.lock").exists()


@pytest.mark.parametrize("create", [False, True])
def test_postcommit_fsync_failure_reports_commit_and_keeps_backup(pool, monkeypatch, create):
    path, data, raw = pool
    if create:
        path.unlink()
    real_fsync, calls = os.fsync, []
    def fsync(descriptor):
        calls.append(1)
        if len(calls) == (2 if create else 4):
            raise OSError("directory sync failed")
        return real_fsync(descriptor)
    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(io.ConfigCommittedError) as caught:
        io.edit_document(path, lambda _: _bump(data), create=create)
    error = caught.value
    assert error.committed and "committed" in str(error)
    assert io.read_document(path)[0]["accounts"][0]["priority"] == 1
    assert path.stat().st_nlink == 1
    if not create:
        assert Path(error.report["backup_file"]).read_bytes() == raw
    assert not any(".temp-" in name for name in _artifacts(path))


def test_init_publish_is_exclusive_even_after_final_check(pool, monkeypatch):
    path, data, raw = pool
    path.unlink()
    real_link = os.link
    def link(*args, **kwargs):
        path.write_bytes(raw)
        return real_link(*args, **kwargs)
    monkeypatch.setattr(os, "link", link)
    with pytest.raises(FileExistsError):
        io.edit_document(path, lambda _: _bump(data), create=True)
    assert path.read_bytes() == raw and not _artifacts(path)


def _worker(path, barrier, queue):
    barrier.wait(timeout=10)
    busy = 0
    for _ in range(4):
        deadline = time.monotonic() + 10
        while True:
            try:
                def transform(data):
                    time.sleep(0.01)
                    return _bump(data)
                io.edit_document(Path(path), transform)
                break
            except io.ConfigBusyError:
                busy += 1
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.005)
    queue.put(busy)


def test_process_writers_retry_busy_without_lost_updates(pool):
    path, _, _ = pool
    context = multiprocessing.get_context("fork")
    barrier, queue = context.Barrier(3), context.Queue()
    processes = [context.Process(target=_worker, args=(str(path), barrier, queue))
                 for _ in range(3)]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert sum(queue.get(timeout=2) for _ in processes) > 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        queue.close()
    assert io.read_document(path)[0]["accounts"][0]["priority"] == 12
    backups = list(path.parent.glob(".pool.yaml.backup-*"))
    assert len(backups) == 12
    assert {yaml.safe_load(p.read_bytes())["accounts"][0]["priority"] for p in backups} == set(range(12))


def test_live_lock_is_nonblocking_and_never_removed(pool):
    path, _, raw = pool
    lock = path.with_name(".pool.yaml.lock")
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        inode = lock.stat().st_ino
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(io.ConfigBusyError):
            io.edit_document(path, _bump)
        assert lock.stat().st_ino == inode and path.read_bytes() == raw
        assert io.edit_document(path, _bump, dry_run=True)["changed"]
    finally:
        os.close(descriptor)
    io.edit_document(path, _bump)
    assert lock.stat().st_ino == inode


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm", "/child"])
@pytest.mark.parametrize("field", ["token", "credential"])
def test_io_enforces_shared_resource_path_validation(pool, suffix, field):
    path, _, raw = pool
    state = path.parent / "runtime" / "llm-pools" / "operations" / "state.sqlite3"
    def transform(data):
        if field == "token":
            data["gateway"]["token_file"] = str(state) + suffix
        else:
            data["accounts"][0]["credential_file"] = str(state) + suffix
        return data
    with pytest.raises(ValueError):
        io.edit_document(path, transform)
    assert path.read_bytes() == raw and not _artifacts(path)
    assert not (path.parent / "runtime").exists()


def test_hash_check_detects_changed_bytes_with_identical_metadata(pool, monkeypatch):
    path, _, raw = pool
    external = raw.replace(b"priority: 0", b"priority: 9")
    signature = io._signature(path.stat())
    real_read, calls = io._read, []
    def read(directory, name):
        calls.append(1)
        if len(calls) == 2:
            path.write_bytes(external)
        contents, _ = real_read(directory, name)
        return contents, signature
    monkeypatch.setattr(io, "_read", read)
    with pytest.raises(io.ConfigConflictError):
        io.edit_document(path, _bump)
    assert path.read_bytes() == external and not _artifacts(path)
