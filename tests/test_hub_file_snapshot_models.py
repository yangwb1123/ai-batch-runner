"""Strict snapshot DTO validation without reinterpreting file bytes."""

import base64
import hashlib

import pytest

from pbatch.hub_file_snapshot_models import (
    MAX_FILE_BYTES, SCHEMA, content_kind, rejects_secret_content,
    validate_snapshot_paths, validate_upload,
)


def file_row(raw=b"hello\n", path="report.txt", snapshot_id="a" * 32):
    return {"snapshot_id": snapshot_id, "path": path,
            "content_b64": base64.b64encode(raw).decode("ascii"),
            "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}


def upload(**changes):
    return {"schema": SCHEMA, "instance_id": "alpha", "generation": 1,
            "session_id": "session", "turn_id": "turn", "snapshot_set_id": "b" * 32,
            "files": [file_row()], "unavailable": [], **changes}


@pytest.mark.parametrize("raw,kind", [(b"", "text"), ("中文\n".encode(), "text"),
                                      (b"\x00\xff\x03", "binary")])
def test_preserves_exact_bytes_including_empty_and_binary(raw, kind):
    body = upload(files=[file_row(raw)])
    assert validate_upload(body) == body
    assert content_kind(raw) == kind


@pytest.mark.parametrize("paths", [["../a"], ["a//b"], ["A", "a"], ["a", "a/b"],
                                  ["/a"], ["a\\b"], ["C:foo"], ["x/CON.txt"],
                                  [".git/config"], ["dir/.env.local"], [".ssh/key"],
                                  [".docker/config.json"], [".git-credentials"],
                                  [".vault-token"], [".bash_history"], [".zsh_history"],
                                  ["secrets.json"], ["key.pem"], ["a\u202eb.txt"],
                                  ["e\u0301.txt"], ["a" + str(i) for i in range(9)]])
def test_rejects_unsafe_private_or_colliding_paths(paths):
    with pytest.raises(ValueError):
        validate_snapshot_paths(paths)


def test_path_order_and_nonsecret_hidden_file_are_preserved():
    assert validate_snapshot_paths(["z.txt", ".gitignore", "a.txt"]) == ["z.txt", ".gitignore", "a.txt"]


@pytest.mark.parametrize("changes", [{"generation": True}, {"generation": "1"},
                                    {"snapshot_set_id": "../x"}, {"unknown": 1},
                                    {"files": [], "unavailable": []},
                                    {"unavailable": [{"path": "a", "code": "unknown"}]}])
def test_rejects_invalid_upload_envelope(changes):
    with pytest.raises(ValueError):
        validate_upload(upload(**changes))


@pytest.mark.parametrize("changes", [{"sha256": "0" * 64}, {"size_bytes": True},
                                    {"size_bytes": 99}, {"content_b64": "aGVsbG8=\n"},
                                    {"snapshot_id": "x" * 32}, {"extra": 1}])
def test_rejects_file_integrity_and_shape_changes(changes):
    with pytest.raises(ValueError):
        validate_upload(upload(files=[{**file_row(), **changes}]))


def test_bounds_total_bytes_and_declared_paths():
    files = [file_row(b"x" * MAX_FILE_BYTES, str(i), str(i) * 32) for i in range(5)]
    with pytest.raises(ValueError):
        validate_upload(upload(files=files))
    with pytest.raises(ValueError):
        validate_upload(upload(files=[file_row(b"x" * (MAX_FILE_BYTES + 1))]))
    with pytest.raises(ValueError):
        validate_upload(upload(unavailable=[{"path": "REPORT.TXT", "code": "missing"}]))
    with pytest.raises(ValueError):
        validate_upload(upload(files=[file_row(), file_row(path="other")]))


@pytest.mark.parametrize("raw", [b"token=private-value", b"Bearer abcdefghijklmnop",
                                b"-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
                                b"-----BEGIN PRIVATE KEY-----\nsecret",
                                "token=private-value".encode("utf-16")])
def test_known_secrets_reject_without_redacting_original_bytes(raw):
    assert rejects_secret_content(raw)
    with pytest.raises(ValueError):
        validate_upload(upload(files=[file_row(raw)]))
