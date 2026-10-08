"""Closed USTAR parsing, full member digests, private publication and bounded views."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile
from types import SimpleNamespace
import urllib.error

import pytest

from test_task_directory_preservation import saved_tree, source, _copy, _blob
from test_task_retention import _view, _keep
from test_task_center_http import server, request
from test_task_center_browser import run_browser
from pbatch import task_directory_archive as archive, task_directory_inventory as inventory
from pbatch import task_directory_preservation as directory, task_preservation as preservation
from pbatch import task_preservation_io as io, task_retention_store as store, task_center
from pbatch.task_preservation_schema import validate_receipt, receipt_key
from pbatch.task_directory_schema import member_path


def _header(name=".", *, kind=tarfile.DIRTYPE, size=0):
    item = tarfile.TarInfo(name)
    item.type, item.size, item.mode = kind, size, 0o700
    return item


def _container(headers):
    raw = b""
    for header in headers:
        raw += header.tobuf(tarfile.USTAR_FORMAT, "utf-8", "strict")
        raw += b"x" * header.size + bytes((-header.size) % 512)
    raw += bytes(1024)
    return raw + bytes((-len(raw)) % 10240)


def _rewrite(value, raw, *, summary=None):
    value = copy.deepcopy(value)
    value["payload"] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if summary is not None:
        value["directory_copy"] = summary
    value["preservation_id"] = receipt_key(value)
    _blob(value).write_bytes(raw)
    _blob(value).chmod(0o600)
    preservation.receipt_path(value["preservation_id"]).write_text(json.dumps(value))
    preservation.receipt_path(value["preservation_id"]).chmod(0o600)
    return value


@pytest.mark.parametrize("damage", ["no_root", "absolute", "traversal", "double_slash", "backslash", "git", "duplicate", "order", "parent", "file_parent", "link", "pax", "owner", "privileged", "directory_body"])
def test_full_payload_hash_alone_does_not_admit_unsafe_or_unsupported_members(saved_tree, damage):
    value = _copy(saved_tree)
    root = _header()
    child = _header("file", kind=tarfile.REGTYPE)
    headers = [root, child]
    if damage == "no_root":
        headers = [child]
    elif damage in {"absolute", "traversal", "double_slash", "backslash", "git"}:
        child.name = {"absolute": "/outside", "traversal": "../outside", "double_slash": "a//b",
                      "backslash": "..\\outside", "git": ".git"}[damage]
    elif damage == "duplicate":
        headers.append(child)
    elif damage == "order":
        headers = [root, _header("z"), _header("a")]
    elif damage == "parent":
        child.name = "missing/child"
    elif damage == "file_parent":
        headers.append(_header("file/child", kind=tarfile.REGTYPE))
    elif damage == "link":
        child.type, child.linkname = tarfile.SYMTYPE, "/outside/private"
    elif damage == "pax":
        child.type = tarfile.XHDTYPE
    elif damage == "owner":
        child.uid = 123
    elif damage == "privileged":
        child.mode = 0o4700
    else:
        child.type, child.size = tarfile.DIRTYPE, 1
    fabricated = _rewrite(value, _container(headers))
    with pytest.raises(ValueError, match="directory copy"):
        preservation.verify_copy(fabricated["preservation_id"], confirm=True)
    assert _blob(value).exists()  # no automatic salvage, overwrite or deletion


@pytest.mark.parametrize("damage", ["body_padding", "trailer", "truncated", "extra_zeros", "checksum"])
def test_noncanonical_incomplete_or_extra_bytes_refuse_even_with_updated_global_hash(saved_tree, damage):
    value = _copy(saved_tree)
    raw = bytearray(_blob(value).read_bytes())
    if damage == "body_padding":
        # Root + two directories + nested file header; first file body has padding.
        header = tarfile.TarInfo.frombuf(bytes(raw[1536:2048]), "utf-8", "strict")
        raw[2048 + header.size] = 1
    elif damage == "trailer":
        raw[-1] = 1
    elif damage == "truncated":
        raw = raw[:-10240]  # empty/shortened: schema/container check must also refuse
    elif damage == "extra_zeros":
        raw.extend(bytes(10240))
    else:
        raw[148] = ord("x")
    fabricated = _rewrite(value, bytes(raw))
    with pytest.raises(ValueError):
        preservation.verify_copy(fabricated["preservation_id"], confirm=True)


def test_valid_container_with_fabricated_count_or_member_hash_is_not_a_matching_directory_copy(saved_tree):
    value = _copy(saved_tree)
    for field in ("members_sha256", "files", "content_bytes"):
        summary = dict(value["directory_copy"])
        if field == "members_sha256":
            summary[field] = "f" * 64
        elif field == "files":
            summary["files"] -= 1
            summary["directories"] += 1
        else:
            summary[field] += 1
        fabricated = _rewrite(value, _blob(value).read_bytes(), summary=summary)
        with pytest.raises(ValueError, match="members/counts"):
            preservation.verify_copy(fabricated["preservation_id"], confirm=True)


@pytest.mark.parametrize("field, replacement", [("schema_version", True), ("schema_version", 3),
    ("source_stat", {"mode": 0o100600}), ("directory_copy", {}), ("restore", True)])
def test_schema_two_is_closed_and_not_an_authenticated_restore_or_recovery_attestation(saved_tree, field, replacement):
    value = _copy(saved_tree)
    value[field] = replacement
    with pytest.raises(ValueError):
        validate_receipt(value)


def test_saved_payload_or_receipt_change_during_member_verification_is_not_published_as_matching(saved_tree, monkeypatch):
    value = _copy(saved_tree)
    original = archive._body
    def racing(handle, size):
        result = original(handle, size)
        _blob(value).write_bytes(b"replacement is retained, not repaired")
        return result
    monkeypatch.setattr(archive, "_body", racing)
    with pytest.raises(ValueError):
        preservation.verify_copy(value["preservation_id"], confirm=True)
    assert _blob(value).read_bytes() == b"replacement is retained, not repaired"


def test_foreign_uid_and_cross_device_entries_refuse_without_inferring_ownership_or_mount_safety():
    for changes in ({"st_uid": -1}, {"st_dev": 2}, {"st_mode": stat.S_IFSOCK | 0o600}, {"st_nlink": 2}):
        info = SimpleNamespace(**({"st_uid": os.geteuid(), "st_dev": 1, "st_mode": stat.S_IFREG | 0o600, "st_nlink": 1} | changes))
        with pytest.raises(ValueError):
            inventory._admit(info, 1)


def test_directory_descriptor_to_named_path_replacement_is_not_adopted(saved_tree, monkeypatch):
    original = inventory.os.scandir
    done = False
    def replacement(fd):
        nonlocal done
        if not done:
            done = True
            saved_tree[1].rename(saved_tree[1].with_name("original-preserved"))
            saved_tree[1].mkdir()
        return original(fd)
    monkeypatch.setattr(inventory.os, "scandir", replacement)
    with pytest.raises(ValueError, match="changed"):
        _copy(saved_tree)
    assert not list(preservation.preservation_root().glob("pp-*"))


def test_publication_failure_keeps_complete_orphan_and_never_overwrites_or_retries_it(saved_tree, monkeypatch):
    original = io.os.link
    def interrupted(source, target):
        if Path(target).suffix == ".json":
            raise OSError("owned synthetic directory receipt publication failure")
        return original(source, target)
    with monkeypatch.context() as scope:
        scope.setattr(io.os, "link", interrupted)
        with pytest.raises(OSError, match="publication failure"):
            _copy(saved_tree)
    blobs = list(preservation.preservation_root().glob("pp-*.blob"))
    assert len(blobs) == 1 and len(blobs[0].read_bytes()) >= 10240
    before = blobs[0].read_bytes()
    with pytest.raises(ValueError, match="collision"):
        _copy(saved_tree)
    assert blobs[0].read_bytes() == before and not list(preservation.preservation_root().glob("*.json"))


def test_shared_receipt_capacity_never_deletes_copies_and_accepts_identical_noop_at_capacity(saved_tree, monkeypatch):
    value = _copy(saved_tree)
    monkeypatch.setattr(preservation, "MAX_RECEIPTS", 1)
    assert _copy(saved_tree) == value
    (saved_tree[1] / "run.sh").write_text("new bytes")
    with pytest.raises(ValueError, match="capacity"):
        _copy(saved_tree)
    assert _blob(value).exists()


def test_ordinary_views_and_authenticated_http_only_use_bounded_receipt_metadata(server, saved_tree, monkeypatch):
    value = _copy(saved_tree)
    monkeypatch.setattr(archive, "inventory", lambda *a: pytest.fail("query enumerated directory contents"))
    monkeypatch.setattr(archive, "verify_directory", lambda *a: pytest.fail("query read saved members"))
    monkeypatch.setattr(preservation, "verify_copy", lambda *a, **k: pytest.fail("query verified saved bytes"))
    expected = _view(saved_tree)
    public = expected["preservation"]["receipts"][0]
    assert public["directory_copy"] == value["directory_copy"]
    assert public["current_payload_reverified"] is public["current_source_reverified"] is False
    assert "private-body.txt" not in json.dumps(expected) and "PRIVATE-DIRECTORY-BODY" not in json.dumps(expected)
    _keep(expected)
    with request(server, "/api/resources/" + saved_tree[2]["resource_id"] + "?preserve-directory=true&verify-copy=true&confirm=true") as response:
        assert json.loads(response.read()) == expected
    for action in ("preserve-directory", "restore", "verify-copy", "download"):
        with pytest.raises(urllib.error.HTTPError) as exc:
            request(server, "/api/resources/" + saved_tree[2]["resource_id"] + "/" + action, method="POST")
        assert exc.value.code == 403


@pytest.mark.skipif(shutil.which("node") is None, reason="isolated DOM fixture needs Node")
def test_web_renders_directory_summary_as_text_without_private_members_bodies_or_restore_controls(saved_tree):
    value = _copy(saved_tree)
    view = task_center.snapshot()
    marker = '<svg onload="evil()">quoted directory-copy reason</svg>'
    view["resources"][0]["preservation"]["receipts"][0]["reason"] = marker
    result = run_browser([view], "resource")
    row = json.loads(result["detail"])
    assert result["tags"] == 0 and len(result["requests"]) == 1
    assert row["preservation"]["receipts"][0]["directory_copy"] == value["directory_copy"]
    assert row["preservation"]["receipts"][0]["reason"] == marker
    assert "private-body.txt" not in result["all"] and "PRIVATE-DIRECTORY-BODY" not in result["all"]
    _keep(row)


@pytest.mark.parametrize("path", ["../x", "/absolute", "a//b", "a/./b", "a/../b", "a\\b", "C:drive", ".git", "a/.git/config", "x\udcff", "x\x00", "x\u2066", "x" * 100])
def test_member_path_admission_never_normalizes_or_salvages_unsafe_names(path):
    with pytest.raises(ValueError):
        member_path(path)
