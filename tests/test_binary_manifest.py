"""Binary binding fails closed on stale artifacts and source inventory drift."""

from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import threading

import pytest


ROOT = Path(__file__).resolve().parent.parent
BUILDER = runpy.run_path(str(ROOT / "tools/build-binary.py"))
MANIFEST = BUILDER["binary_manifest"]


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name in MANIFEST.ASSET_FILES:
        (root / name).write_text("original " + name + "\n", encoding="utf-8")
    for name in MANIFEST.RECIPE_FILES:
        target = root / name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes((ROOT / name).read_bytes())
    for name in ("pbatch/__init__.py", "pbatch/nested/model.py", "pbatch/task_keywords.yaml",
                 "docs/nested/contract.md", "scripts/check.sh"):
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original " + name + "\n", encoding="utf-8")
    return root


def _fake_builder(monkeypatch, callback=None):
    commands = []
    def compile(command, **kwargs):
        commands.append(command)
        output = Path(command[command.index("--distpath") + 1])
        name = command[command.index("--name") + 1]
        if BUILDER["platform"].system().lower() == "windows":
            name += ".exe"
        (output / name).write_bytes(b"frozen fixture")
        if callback is not None:
            callback(Path(kwargs["cwd"]), output)
    monkeypatch.setattr(BUILDER["subprocess"], "run", compile)
    return commands


def _build(source, tmp_path, monkeypatch):
    commands = _fake_builder(monkeypatch)
    binary = BUILDER["build"](tmp_path / "candidate", source, "pi-batch-test")
    manifest, license_file = MANIFEST.sidecars(binary)
    return binary, manifest, license_file, commands


def test_build_binds_snapshot_binary_license_and_environment(source, tmp_path, monkeypatch):
    binary, manifest, license_file, commands = _build(source, tmp_path, monkeypatch)
    data = json.loads(manifest.read_text())
    assert data["schema_version"] == 1
    assert data["source"]["files"] == MANIFEST.source_snapshot(source)["files"]
    assert data["binary"] == {"name": binary.name, **MANIFEST.file_record(binary)}
    assert data["environment"]["python"] and data["environment"]["platform"]
    assert "no test, production readiness or license clearance claim" in data["scope"]
    assert license_file.read_bytes() == (source / "LICENSE").read_bytes()
    assert MANIFEST.verify(manifest, source)["ok"]
    assert "--collect-data" not in commands[0]
    mappings = [commands[0][index + 1] for index, arg in enumerate(commands[0]) if arg == "--add-data"]
    assert any(value.endswith(os.pathsep + "share/ai-batch-runner") and "/LICENSE" in value
               for value in mappings)
    assert all(not (source.as_posix() + "/") in value for value in mappings)
    assert not list(binary.parent.glob("." + binary.name + "-build-*"))


@pytest.mark.parametrize("change", ["modify", "add", "delete"])
def test_verification_detects_source_contents_and_directory_inventory_drift(
        source, tmp_path, monkeypatch, change):
    _, manifest, _, _ = _build(source, tmp_path, monkeypatch)
    target = source / "docs/nested/contract.md"
    if change == "modify":
        target.write_text("new contents")
    elif change == "add":
        (source / "pbatch/new_module.py").write_text("new module")
    elif change == "delete":
        target.unlink()
    report = MANIFEST.verify(manifest, source)
    assert not report["ok"] and any("source" in error for error in report["errors"])


@pytest.mark.parametrize("target", ["binary", "license"])
def test_verification_rejects_replaced_artifact_or_license(source, tmp_path, monkeypatch, target):
    binary, manifest, license_file, _ = _build(source, tmp_path, monkeypatch)
    (binary if target == "binary" else license_file).write_bytes(b"replaced")
    assert MANIFEST.verify(manifest, source)["errors"] == [target + " content differs"]


@pytest.mark.parametrize("change", ["modify", "add", "delete", "staged"])
def test_build_drift_removes_old_success_evidence_and_never_publishes_new_manifest(
        source, tmp_path, monkeypatch, change):
    output = tmp_path / "candidate"
    output.mkdir()
    old_manifest = output / "pi-batch.manifest.json"
    old_manifest.write_text('{"old_success":true}')
    original = (source / "pbatch/__init__.py").read_bytes()
    def change_input(staged, output):
        assert not old_manifest.exists()
        assert (staged / "pbatch/__init__.py").read_bytes() == original
        if change == "modify":
            (source / "pbatch/__init__.py").write_bytes(b"changed")
        elif change == "add":
            (source / "docs/new.md").write_bytes(b"new")
        elif change == "delete":
            (source / "docs/nested/contract.md").unlink()
        else:
            (staged / "pbatch/__init__.py").write_bytes(b"changed")
    _fake_builder(monkeypatch, change_input)
    with pytest.raises(RuntimeError, match="source inputs changed"):
        BUILDER["build"](output, source)
    assert not old_manifest.exists()


def test_build_failure_invalidates_previous_binding(source, tmp_path, monkeypatch):
    binary, manifest, _, _ = _build(source, tmp_path, monkeypatch)
    def failure(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0])
    monkeypatch.setattr(BUILDER["subprocess"], "run", failure)
    with pytest.raises(subprocess.CalledProcessError):
        BUILDER["build"](binary.parent, source, "pi-batch-test")
    assert not manifest.exists()


def test_generated_directories_are_excluded_from_both_inventory_and_build_mappings(source):
    for name in ("docs/dist/output", "docs/.git/state", "pbatch/__pycache__/code.pyc",
                 "examples/build/generated", "dist/binary", "pbatch/ignored.pyc"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"generated")
    snapshot = MANIFEST.source_snapshot(source)
    assert not any("generated" in name or name.endswith(".pyc") for name in snapshot["files"])
    paths = {str(path.relative_to(source)) for path, _ in MANIFEST.data_mappings(source, snapshot)}
    assert paths == set(snapshot["files"]) - set(MANIFEST.RECIPE_FILES)
    assert "docs/dist/output" not in paths and "docs/.git/state" not in paths
    package_data = [(path.relative_to(source).as_posix(), destination)
                    for path, destination in MANIFEST.data_mappings(source, snapshot)]
    assert ("pbatch/task_keywords.yaml", "pbatch") in package_data
    assert ("pbatch/task_keywords.yaml", "share/ai-batch-runner/pbatch") in package_data


@pytest.mark.parametrize("directory", [False, True])
def test_source_symlinks_are_rejected_before_packaging(source, directory):
    target = source / "docs/nested" if directory else source / "LICENSE"
    (source / "docs/link").symlink_to(target, target_is_directory=directory)
    with pytest.raises(ValueError, match="symlink"):
        MANIFEST.source_snapshot(source)


def test_build_refuses_output_inside_packaged_sources(source, monkeypatch):
    commands = _fake_builder(monkeypatch)
    with pytest.raises(ValueError, match="packaged source directory"):
        BUILDER["build"](source / "docs/candidate", source)
    assert commands == []


def _git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args], stderr=subprocess.STDOUT)


def test_git_dirty_ignores_unpacked_generated_output_but_includes_build_inputs(source):
    _git(source, "init", "-q")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "source")
    snapshot = MANIFEST.source_snapshot(source)
    clean = MANIFEST.git_record(source, snapshot)
    assert clean["available"] and len(clean["commit"]) == 40 and clean["dirty"] is False
    (source / "validation").mkdir()
    (source / "validation/build.log").write_text("generated output")
    assert MANIFEST.git_record(source, snapshot)["dirty"] is False
    (source / "pbatch/new.py").write_text("new build input")
    assert MANIFEST.git_record(source, MANIFEST.source_snapshot(source))["dirty"] is True
    (source / "pbatch/new.py").unlink()
    (source / "LICENSE").write_text("changed tracked file")
    assert MANIFEST.git_record(source, MANIFEST.source_snapshot(source))["dirty"] is True


def test_gitignored_runtime_documents_and_empty_directories_are_not_packaged(source):
    _git(source, "init", "-q")
    (source / ".gitignore").write_text("docs/reviews/\ndocs/backend/backend-quality.json\n")
    expected = MANIFEST.source_snapshot(source)
    for name in ("docs/reviews/report.md", "docs/backend/backend-quality.json"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("local runtime report must not ship")
    (source / "docs/empty-runtime-directory").mkdir()
    assert MANIFEST.source_snapshot(source) == expected
    (source / "pbatch/new.py").write_text("new authored source is included")
    assert "pbatch/new.py" in MANIFEST.source_snapshot(source)["files"]


def test_manifest_cli_reports_missing_artifacts_without_claiming_success(
        source, tmp_path, monkeypatch, capsys):
    binary, manifest, _, _ = _build(source, tmp_path, monkeypatch)
    binary.unlink()
    code = MANIFEST.main(["verify", "--manifest", str(manifest), "--source-root", str(source)])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["ok"] is False and result["errors"]


@pytest.mark.parametrize("document", [[], {}, {"schema_version": 2}, "invalid json"])
def test_manifest_cli_rejects_invalid_evidence_as_json(source, tmp_path, capsys, document):
    manifest = tmp_path / "invalid.manifest.json"
    manifest.write_text(document if isinstance(document, str) else json.dumps(document))
    code = MANIFEST.main(["verify", "--manifest", str(manifest), "--source-root", str(source)])
    result = json.loads(capsys.readouterr().out)
    assert code == 1 and result["ok"] is False


def test_verification_rejects_different_git_commit_even_for_unchanged_inputs(
        source, tmp_path, monkeypatch):
    _git(source, "init", "-q")
    _git(source, "add", ".")
    author = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
    _git(source, *author, "commit", "-qm", "source")
    with monkeypatch.context() as patched:
        _, manifest, _, _ = _build(source, tmp_path, patched)
    assert MANIFEST.verify(manifest, source)["ok"]
    _git(source, *author, "commit", "--allow-empty", "-qm", "new provenance")
    assert MANIFEST.verify(manifest, source)["errors"] == ["source git commit differs"]


def test_dependency_inventory_only_records_external_analysis_inputs(source, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    external = tmp_path / "dependency.py"
    external.write_bytes(b"external dependency")
    local_dependency = source / ".venv/lib/dependency.py"
    local_dependency.parent.mkdir(parents=True)
    local_dependency.write_bytes(b"dependency inside source checkout")
    generated = work / "generated.py"
    generated.write_bytes(b"generated")
    values = ([("external", str(external), "PYMODULE"),
               ("source", str(source / "pbatch/__init__.py"), "PYMODULE")],
              [("generated", str(generated), "PYMODULE"),
               ("venv", str(local_dependency), "PYMODULE")])
    (work / "Analysis-00.toc").write_text(repr(values))
    assert MANIFEST.dependency_records(work, source) == [
        {"path": str(path), **MANIFEST.file_record(path)} for path in sorted((external, local_dependency))]


def _commit_source(source):
    _git(source, "init", "-q")
    _git(source, "add", ".")
    _git(source, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "source")


def test_successful_compiler_without_output_cannot_bind_existing_binary(
        source, tmp_path, monkeypatch):
    _commit_source(source)
    assert MANIFEST.git_record(source, MANIFEST.source_snapshot(source))["dirty"] is False
    output = tmp_path / "candidate"
    output.mkdir()
    binary = output / "pi-batch"
    binary.write_bytes(b"old binary from an unrelated source")
    manifest, _ = MANIFEST.sidecars(binary)
    manifest.write_text('{"old_success":true}')
    monkeypatch.setattr(BUILDER["subprocess"], "run", lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="did not produce a new executable"):
        BUILDER["build"](output, source)
    assert binary.read_bytes() == b"old binary from an unrelated source"
    assert not manifest.exists()


def _run_build(errors, output, source, name="pi-batch"):
    try:
        BUILDER["build"](output, source, name)
    except BaseException as exc:
        errors.append(exc)


def test_same_artifact_concurrent_build_is_refused_before_wrong_source_can_publish(
        source, tmp_path, monkeypatch):
    other = tmp_path / "other-source"
    shutil.copytree(source, other)
    (other / "pbatch/__init__.py").write_text("different source")
    _commit_source(source)
    _commit_source(other)
    entered, release = threading.Event(), threading.Event()
    errors, calls = [], []
    def pause_after_compile(staged, output):
        calls.append(staged)
        (output / "pi-batch").write_bytes((staged / "pbatch/__init__.py").read_bytes())
        entered.set()
        assert release.wait(5)
    _fake_builder(monkeypatch, pause_after_compile)
    output = tmp_path / "candidate"
    worker = threading.Thread(target=_run_build, args=(errors, output, source))
    worker.start()
    try:
        assert entered.wait(5)
        with pytest.raises(MANIFEST.BuildBusyError, match="already in progress"):
            BUILDER["build"](output, other)
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and errors == [] and len(calls) == 1
    binary = output / "pi-batch"
    assert binary.read_bytes() == (source / "pbatch/__init__.py").read_bytes()
    evidence = json.loads(MANIFEST.sidecars(binary)[0].read_text())
    assert evidence["source"]["git"]["dirty"] is False
    assert MANIFEST.verify(MANIFEST.sidecars(binary)[0], source)["ok"]
    assert not MANIFEST.verify(MANIFEST.sidecars(binary)[0], other)["ok"]


def test_busy_cli_keeps_existing_evidence_and_stable_lock(source, tmp_path, capsys):
    output = tmp_path / "candidate"
    output.mkdir()
    binary = output / "pi-batch"
    manifest, _ = MANIFEST.sidecars(binary)
    manifest.write_bytes(b"active builder evidence")
    with MANIFEST.output_lock(binary):
        assert BUILDER["main"](["--output", str(output)]) == 5
        assert manifest.read_bytes() == b"active builder evidence"
    assert "already in progress" in capsys.readouterr().err
    lock = output / ".pi-batch.build.lock"
    identity = lock.stat().st_ino
    with MANIFEST.output_lock(binary):
        assert lock.stat().st_ino == identity
    assert lock.stat().st_ino == identity


def test_different_artifacts_build_concurrently_with_isolated_work_and_dependencies(
        source, tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    commands, errors = [], []
    def compile(command, **kwargs):
        commands.append(command)
        name = command[command.index("--name") + 1]
        output = Path(command[command.index("--distpath") + 1])
        work = Path(command[command.index("--workpath") + 1])
        work.mkdir()
        dependency = tmp_path / (name + ".dependency")
        dependency.write_text(name)
        (work / "Analysis-00.toc").write_text(repr([(name, str(dependency), "DATA")]))
        (output / name).write_text("compiled:" + name)
        barrier.wait(5)
    monkeypatch.setattr(BUILDER["subprocess"], "run", compile)
    output = tmp_path / "candidate"
    workers = [threading.Thread(target=_run_build, args=(errors, output, source, name))
               for name in ("pi-batch-a", "pi-batch-b")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(8)
    assert not any(worker.is_alive() for worker in workers) and errors == []
    for option in ("--distpath", "--workpath", "--specpath"):
        assert len({command[command.index(option) + 1] for command in commands}) == 2
    for name in ("pi-batch-a", "pi-batch-b"):
        binary = output / name
        assert binary.read_text() == "compiled:" + name
        manifest = MANIFEST.sidecars(binary)[0]
        assert MANIFEST.verify(manifest, source)["ok"]
        evidence = json.loads(manifest.read_text())
        assert [Path(item["path"]).name for item in evidence["build_dependencies"]] == [name + ".dependency"]


@pytest.mark.parametrize("failed_step", ["binary", "license", "manifest"])
def test_publication_failure_never_leaves_success_evidence_and_releases_lock(
        source, tmp_path, monkeypatch, failed_step):
    binary, manifest, license_file, _ = _build(source, tmp_path, monkeypatch)
    target = {"binary": binary, "license": license_file, "manifest": manifest}[failed_step]
    original = MANIFEST.os.replace
    def fail_publication(source_path, target_path):
        if Path(target_path) == target:
            raise PermissionError("injected publication failure")
        return original(source_path, target_path)
    with monkeypatch.context() as patch:
        patch.setattr(MANIFEST.os, "replace", fail_publication)
        with pytest.raises(PermissionError, match="publication failure"):
            BUILDER["build"](binary.parent, source, binary.name)
    assert not manifest.exists()
    BUILDER["build"](binary.parent, source, binary.name)
    assert MANIFEST.verify(manifest, source)["ok"]


def test_git_becoming_dirty_during_build_cannot_keep_clean_manifest(source, tmp_path, monkeypatch):
    (source / "maintenance.txt").write_text("tracked but not packaged")
    _commit_source(source)
    def make_dirty(staged, output):
        (source / "maintenance.txt").write_text("changed while compiling")
    _fake_builder(monkeypatch, make_dirty)
    output = tmp_path / "candidate"
    with pytest.raises(RuntimeError, match="source inputs changed"):
        BUILDER["build"](output, source)
    assert not (output / "pi-batch.manifest.json").exists()


def test_atomic_file_replacement_while_hashing_is_detected(tmp_path, monkeypatch):
    path, replacement = tmp_path / "candidate", tmp_path / "replacement"
    path.write_bytes(b"original")
    replacement.write_bytes(b"different")
    original_hash = MANIFEST.hashlib.sha256
    class ReplacingHash:
        def __init__(self):
            self.digest = original_hash()
        def update(self, value):
            self.digest.update(value)
            os.replace(replacement, path)
        def hexdigest(self):
            return self.digest.hexdigest()
    monkeypatch.setattr(MANIFEST.hashlib, "sha256", ReplacingHash)
    with pytest.raises(RuntimeError, match="changed while hashing"):
        MANIFEST.file_record(path)
