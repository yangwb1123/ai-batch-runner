"""Lock generations, declaration bounds and interrupted participant publication."""
from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import cli, lock, lock_storage


@pytest.fixture
def coordination(tmp_path, monkeypatch):
    path = tmp_path / "instance.lock"
    monkeypatch.setenv("PBATCH_LOCK_FILE", str(path))
    monkeypatch.setattr(lock, "_worktree_dirty", lambda _cwd: False)
    owned = []

    def acquire(**kwargs):
        handle = lock.acquire_lock(str(tmp_path), **kwargs)
        owned.append(handle)
        return handle

    yield path, acquire
    for handle in reversed(owned):
        lock.release_lock(handle)
        # Malformed-holder cases intentionally retain unreadable metadata;
        # discard only this fixture's in-memory ownership after exercising it.
        lock._OWNED.pop(str(handle), None)
        lock._COORDINATION.pop(str(handle), None)


def test_late_old_generation_cannot_verify_or_release_reacquired_base(coordination):
    path, acquire = coordination
    first = acquire()
    lock.release_lock(first)
    second = acquire()
    assert first == second == path
    assert first.owner_token != second.owner_token
    assert not lock.verify_held(first) and lock.verify_held(second)
    lock.release_lock(first)
    assert lock.verify_held(second)
    assert lock_storage.read_holder(path)["token"] == second.owner_token
    lock.release_lock(second)
    assert not path.exists()


def test_old_base_release_preserves_new_base_and_remaining_participant(coordination):
    path, acquire = coordination
    original = acquire(outputs=("a.md",))
    participant = acquire(outputs=("b.md",))
    assert participant != original
    lock.release_lock(original)
    replacement = acquire(outputs=("c.md",))
    assert replacement == original == path
    lock.release_lock(original)
    assert lock.verify_held(replacement) and lock.verify_held(participant)
    with pytest.raises(SystemExit) as caught:
        acquire(outputs=("b.md",))
    assert caught.value.code == lock.LOCK_EXIT_CODE


@pytest.mark.parametrize("overflow_side", ["holder", "request"])
@pytest.mark.parametrize("domains", [("", ""), ("first-domain", "second-domain")])
def test_seventeenth_declared_output_keeps_exclusive_protection(coordination, overflow_side, domains):
    path, acquire = coordination
    many = tuple(f"out-{index}.md" for index in range(16)) + ("shared.md",)
    holder_outputs = many if overflow_side == "holder" else ("shared.md",)
    request_outputs = many if overflow_side == "request" else ("shared.md",)
    held = acquire(outputs=holder_outputs, domain=domains[0])
    metadata = lock_storage.read_holder(path)
    assert metadata["outputs_complete"] is (overflow_side != "holder")
    assert len(metadata["outputs"]) <= 16
    with pytest.raises(SystemExit) as caught:
        acquire(outputs=request_outputs, domain=domains[1])
    assert caught.value.code == lock.LOCK_EXIT_CODE and lock.verify_held(held)
    assert lock_storage.holder_paths(path) == [path]


def test_sixteen_complete_outputs_allow_disjoint_participant(coordination):
    _path, acquire = coordination
    held = acquire(outputs=tuple(f"out-{index}.md" for index in range(16)))
    other = acquire(outputs=("unrelated.md",))
    assert held != other and lock.verify_held(held) and lock.verify_held(other)


def test_legacy_sixteen_outputs_without_completeness_remain_exclusive(coordination):
    path, acquire = coordination
    held = acquire(outputs=tuple(f"out-{index}.md" for index in range(16)))
    metadata = lock_storage.read_holder(path)
    metadata.pop("outputs_complete")
    with lock_storage.lock_guard(path):
        path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        acquire(outputs=("unrelated.md",))
    assert caught.value.code == lock.LOCK_EXIT_CODE and lock.verify_held(held)


@pytest.mark.parametrize("section", ["tasks", "stages"])
def test_cli_retains_overflow_marker_for_lock_exclusivity(tmp_path, section):
    source = tmp_path / "batch.yaml"
    outputs = [f"out-{index}.md" for index in range(30)]
    source.write_text(json.dumps({section: [{"output": output} for output in outputs]}))
    args = SimpleNamespace(output="", prompt="", source=str(source))
    declared = cli._planned_outputs(args)
    assert declared == outputs[:17]
    assert lock._payload(str(tmp_path), outputs=tuple(declared))["outputs_complete"] is False


@pytest.mark.parametrize("value", ["false", 1, 0, None, [], {}])
def test_malformed_completeness_never_authorizes_parallel_execution(coordination, value):
    path, acquire = coordination
    acquire(outputs=("first.md",))
    metadata = lock_storage.read_holder(path)
    metadata["outputs_complete"] = value
    raw = json.dumps(metadata)
    with lock_storage.lock_guard(path):
        path.write_text(raw, encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        acquire(outputs=("second.md",))
    assert caught.value.code == lock.LOCK_EXIT_CODE
    assert path.read_text() == raw


def test_interrupted_participant_temp_is_outside_holder_enumeration(coordination, monkeypatch):
    path, acquire = coordination
    base = acquire(outputs=("base.md",))
    partials = []
    original_unlink = os.unlink

    def interrupted_link(source, target):
        source, target = Path(source), Path(target)
        assert source.parent == path.parent
        assert target.parent == path.with_name(path.name + ".holders")
        source.write_bytes(b'{"pid":')
        partials.append(source)
        raise OSError("simulated interruption before publication")

    def keep_partial(source, *args, **kwargs):
        if Path(source) not in partials:
            return original_unlink(source, *args, **kwargs)

    with monkeypatch.context() as failed_publication:
        failed_publication.setattr(lock_storage.os, "link", interrupted_link)
        failed_publication.setattr(lock_storage.os, "unlink", keep_partial)
        with pytest.raises(SystemExit) as caught:
            acquire(outputs=("failed.md",))
        assert caught.value.code == lock.LOCK_EXIT_CODE
    assert len(partials) == 1 and partials[0].read_bytes() == b'{"pid":'
    assert lock_storage.holder_paths(path) == [path]
    lock.release_lock(base)
    next_base = acquire(outputs=("next.md",))
    next_participant = acquire(outputs=("participant.md",))
    assert lock.verify_held(next_base) and lock.verify_held(next_participant)
    assert partials[0] not in lock_storage.holder_paths(path)
    assert partials[0].exists(), "unpublished evidence was unexpectedly removed"
