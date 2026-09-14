"""Contract tests for canonical, portable workspace bundles."""

import base64

import pytest

from pbatch.workspace_bundle import (
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_TOTAL_BYTES,
    SCHEMA,
    WorkspaceBundleError,
    bundle_bytes,
    bundle_digest,
    validate_bundle,
    validate_outputs,
)


def _file(path="a.txt", content=b"a"):
    return {"path": path, "content_b64": base64.b64encode(content).decode("ascii")}


def _bundle(files=()):
    return {"schema": SCHEMA, "files": list(files)}


def test_bundle_sorting_and_cross_language_digest_fixture():
    value = _bundle([_file("z/é.txt", b"\x00\x01"), _file("a.txt", b"a")])

    assert bundle_bytes(value) == (
        '{"files":[{"content_b64":"YQ==","path":"a.txt"},'
        '{"content_b64":"AAE=","path":"z/é.txt"}],'
        '"schema":"pbatch.workspace.v1"}'
    ).encode("utf-8")
    assert bundle_digest(value) == (
        "a388cd8e87940f10987df742843a470dfbdb1b3735fd30e377d2b4b6cf174202"
    )
    assert validate_bundle(value)["files"][0]["path"] == "a.txt"


def test_empty_bundle_and_output_list_are_valid():
    assert validate_bundle(_bundle()) == {"schema": SCHEMA, "files": []}
    assert validate_outputs([]) == []


@pytest.mark.parametrize("value", [
    None,
    {},
    {"schema": "wrong", "files": []},
    {"schema": SCHEMA, "files": [], "extra": True},
    {"schema": SCHEMA, "files": {}},
    {"schema": SCHEMA, "files": [{"path": "a", "content_b64": "YQ==", "mode": 0o777}]},
])
def test_bundle_rejects_unknown_or_malformed_shapes(value):
    with pytest.raises(WorkspaceBundleError):
        validate_bundle(value)


@pytest.mark.parametrize("encoded", ["YQ", "YQ===", "Y Q==", "YQ==\n", "!!!!"])
def test_bundle_rejects_noncanonical_base64(encoded):
    with pytest.raises(WorkspaceBundleError):
        validate_bundle(_bundle([{"path": "a", "content_b64": encoded}]))


@pytest.mark.parametrize("path", [
    "", "/absolute", "C:/drive", "C:relative", "//server/share", "a\\b",
    "a//b", "a/", "./a", "a/./b", "../a", "a/../b", "a\x00b",
    "a\u0085b", "a/CON", "a/con.txt", "a/Lpt9.log", "a/com¹.txt",
    "a/trailing.", "a/trailing ", "a/question?mark", "a/colon:name",
    "a/not-normalized-e\u0301.txt", "a/" + ("x" * 256), "x" * 513,
])
def test_bundle_rejects_unsafe_nonportable_or_noncanonical_paths(path):
    with pytest.raises(WorkspaceBundleError):
        validate_bundle(_bundle([_file(path)]))


@pytest.mark.parametrize("paths", [
    ["A.txt", "a.TXT"],
    ["node", "node/file.txt"],
    ["same.txt", "same.txt"],
])
def test_bundle_rejects_colliding_paths(paths):
    with pytest.raises(WorkspaceBundleError):
        validate_bundle(_bundle([_file(path) for path in paths]))


def test_output_path_validation_uses_bundle_portability_rules():
    assert validate_outputs(["z.txt", "a/file.txt"]) == ["a/file.txt", "z.txt"]
    with pytest.raises(WorkspaceBundleError):
        validate_outputs(["CON.txt"])
    with pytest.raises(WorkspaceBundleError):
        validate_outputs(["root", "root/child"])


def test_bundle_enforces_file_count_individual_and_total_decoded_limits():
    with pytest.raises(WorkspaceBundleError):
        validate_bundle(_bundle([_file(f"f-{index}", b"")
                                 for index in range(MAX_FILES + 1)]))
    with pytest.raises(WorkspaceBundleError):
        validate_bundle(_bundle([_file("large", b"x" * (MAX_FILE_BYTES + 1))]))
    total_files = [_file(f"f-{index}", b"x" * MAX_FILE_BYTES)
                   for index in range(MAX_TOTAL_BYTES // MAX_FILE_BYTES + 1)]
    with pytest.raises(WorkspaceBundleError):
        validate_bundle(_bundle(total_files))


def test_oversized_base64_is_rejected_before_decoding(monkeypatch):
    import pbatch.workspace_bundle as codec

    def unexpected_decode(*args, **kwargs):
        raise AssertionError("oversized base64 must not be decoded")

    monkeypatch.setattr(codec.base64, "b64decode", unexpected_decode)
    with pytest.raises(codec.WorkspaceBundleError, match="size limit"):
        codec.validate_bundle({"schema": codec.SCHEMA, "files": [
            {"path": "large.bin", "content_b64": "A" * (100 * 1024)}]})
