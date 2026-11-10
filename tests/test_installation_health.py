"""Installation diagnostics must not invent project-test evidence."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pbatch import health


def test_explicit_installation_is_a_separate_scope(capsys):
    assert health.health_main(["--installation", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "abr.installation-health.v1"
    assert report["scope"] == "installation"
    assert report["healthy"] is True
    assert "tests" not in report and "score" not in report
    assert all(check["result"] == "passed" for check in report["checks"].values())
    assert report["checks"]["backendquality_fixture"]["files_scanned"] == 2
    assert report["checks"]["backendquality_fixture"]["rejected_violations"] == 1
    assert all(item["result"] == "not_executed" and item["reason"]
               for item in report["not_executed"])


@pytest.fixture
def install():
    return importlib.import_module("pbatch.health_installation")


def test_project_without_tests_remains_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(health, "_checker_violations", lambda *_: 0)
    monkeypatch.setattr(health, "_quality_violations", lambda *_: 0)
    report = health.health_report(tmp_path)
    assert report["tests"] == 0 and report["healthy"] is False


def test_explicit_project_overrides_installed_default(tmp_path, monkeypatch, capsys, install):
    monkeypatch.setattr(install, "installation_kind", lambda: "wheel")
    monkeypatch.setattr(health, "health_report", lambda root: {"healthy": False, "root": str(root)})
    assert health.health_main(["--dir", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {"healthy": False, "root": str(tmp_path)}


def test_installation_and_project_flags_conflict():
    with pytest.raises(SystemExit) as exc:
        health.health_main(["--installation", "--dir", "."])
    assert exc.value.code == 2


def test_default_changes_only_for_owned_installed_package(monkeypatch, capsys, install):
    monkeypatch.setattr(install, "installation_kind", lambda: "wheel")
    monkeypatch.setattr(install, "installation_report", lambda *_: {"healthy": True, "scope": "installation"})
    assert health.health_main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["scope"] == "installation"


@pytest.mark.parametrize("owned", [False, True])
def test_metadata_name_alone_cannot_identify_installation(tmp_path, monkeypatch, install, owned):
    package = tmp_path / "pbatch"
    package.mkdir()
    (package / "__init__.py").write_text("")
    monkeypatch.setattr(install, "PACKAGE_ROOT", package)
    monkeypatch.setattr(install, "is_frozen", lambda: False)
    dist = SimpleNamespace(files=[Path("pbatch/__init__.py")],
                           read_text=lambda name: "pbatch/__init__.py,,0\n" if name == "RECORD" else None,
                           locate_file=lambda p: (tmp_path if owned else tmp_path / "other") / p)
    monkeypatch.setattr(install, "distribution", lambda _: dist)
    assert install.installation_kind() == ("wheel" if owned else "source")


def test_missing_record_does_not_make_missing_tests_an_installation(monkeypatch, install):
    monkeypatch.setattr(install, "is_frozen", lambda: False)
    monkeypatch.setattr(install, "distribution", lambda _: SimpleNamespace(files=None, read_text=lambda _: None))
    assert install.installation_kind() == "source"


@pytest.mark.parametrize("record, editable", [(None, False), ("pbatch/__init__.py,,0\n", True)])
def test_source_manifest_or_editable_record_never_selects_installed_default(monkeypatch, install, record, editable):
    files = [Path("pbatch/__init__.py")]
    metadata = {"RECORD": record, "direct_url.json": json.dumps({"dir_info": {"editable": True}}) if editable else None}
    dist = SimpleNamespace(files=files, read_text=metadata.get,
                           locate_file=lambda path: install.PACKAGE_ROOT.parent / path)
    monkeypatch.setattr(install, "is_frozen", lambda: False)
    monkeypatch.setattr(install, "distribution", lambda _: dist)
    assert install.installation_kind() == "source"


@pytest.mark.parametrize("record, direct", [
    ('"unclosed', None), ("pbatch/__init__.py,,0\nbad-row\n", None),
    ("pbatch/__init__.py,,0\npbatch/__init__.py,,0\n", None),
    ("pbatch/__init__.py,,0\n", '{"dir_info":null}'),
    ("pbatch/__init__.py,,0\n", '{"dir_info":{"editable":true,"editable":false}}'),
    ("pbatch/__init__.py,,0\n", '[' * 2000 + ']' * 2000),
])
def test_malformed_install_metadata_cannot_select_installation(monkeypatch, install, record, direct):
    metadata = {"RECORD": record, "direct_url.json": direct}
    dist = SimpleNamespace(read_text=metadata.get, locate_file=lambda path: install.PACKAGE_ROOT.parent / path)
    monkeypatch.setattr(install, "is_frozen", lambda: False)
    monkeypatch.setattr(install, "distribution", lambda _: dist)
    assert install.installation_kind() == "source"


def test_frozen_runtime_selects_installation(monkeypatch, install):
    monkeypatch.setattr(install, "is_frozen", lambda: True)
    assert install.installation_kind() == "frozen"


@pytest.mark.parametrize("failure", ["empty", "invalid", "wrong-package", "missing-module", "wrong-count", "bool-schema", "exit", "timeout"])
def test_runtime_probe_fails_closed(monkeypatch, install, failure):
    from pbatch.process_exec import ValidationResult
    payload = {"schema_version": 1, "package_root": str(install.PACKAGE_ROOT),
               "modules": list(install.CORE_MODULES), "package_assets": 13}
    if failure == "wrong-package":
        payload["package_root"] = "/unrelated/pbatch"
    if failure == "missing-module":
        payload["modules"].pop()
    if failure == "bool-schema":
        payload["schema_version"] = True
    if failure == "wrong-count":
        payload["package_assets"] = 0
    stdout = "" if failure == "empty" else "invalid" if failure == "invalid" else json.dumps(payload)
    result = ValidationResult(failure not in {"exit", "timeout"}, 1 if failure == "exit" else 0,
                              stdout, "", failure == "timeout")
    monkeypatch.setattr(install, "_run_probe", lambda *_: result)
    assert install._runtime_probe(Path("."), 1000000)["result"] == "failed"


@pytest.mark.parametrize("response", ["", "selfcheck: 0 FAILURES", "prefix selfcheck: OK", "selfcheck: OK\nextra"])
def test_selfcheck_requires_exact_protocol(monkeypatch, install, response):
    from pbatch.process_exec import ValidationResult
    monkeypatch.setattr(install, "_run_probe", lambda *_: ValidationResult(True, 0, response, "", False))
    assert install._selfcheck_probe(Path("."), "knowledge", 1000000)["result"] == "failed"


@pytest.mark.parametrize("change", ["skipped", "zero-files", "bool-total", "missing-total", "false-negative", "bad-json"])
def test_backend_fixture_does_not_accept_empty_scan(monkeypatch, tmp_path, install, change):
    from pbatch.process_exec import ValidationResult
    payload = {"status": "ok", "files_scanned": 1, "total": 0, "violations": []}
    if change == "skipped":
        payload["status"] = "skipped"
    if change == "zero-files":
        payload["files_scanned"] = 0
    if change == "bool-total":
        payload["total"] = False
    if change == "missing-total":
        del payload["total"]
    monkeypatch.setattr(install, "_run_probe", lambda *_: ValidationResult(
        True, 0, "not-json" if change == "bad-json" else json.dumps(payload), "", False))
    assert install._backend_probe(Path("."), tmp_path, 1000000)["result"] == "failed"


def test_missing_distributed_asset_stays_failed(monkeypatch, tmp_path, install):
    monkeypatch.setattr(install, "_runtime_probe", lambda *_: {"result": "passed"})
    monkeypatch.setattr(install, "_selfcheck_probe", lambda *_: {"result": "passed"})
    monkeypatch.setattr(install, "_backend_probe", lambda *_: {"result": "passed"})
    report = install.installation_report(tmp_path)
    assert report["healthy"] is False
    assert report["assets"]["missing"]
    assert report["checks"]["distributed_assets"]["result"] == "failed"


def test_empty_explicit_dir_never_selects_installation(monkeypatch, capsys, install):
    monkeypatch.setattr(install, "installation_kind", lambda: "wheel")
    monkeypatch.setattr(health, "health_report", lambda root: {"healthy": False, "root": str(root)})
    assert health.health_main(["--dir", "", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["root"] == "."


def test_missing_installation_returns_failed_report(tmp_path, install):
    report = install.installation_report(tmp_path)
    assert report["healthy"] is False
    assert all(row["result"] == "failed" for row in report["checks"].values())


def test_probe_has_a_total_deadline_and_owned_cleanup(tmp_path, install):
    import sys
    import time
    start = time.monotonic()
    result = install._run_probe(tmp_path, [sys.executable, "-c", "import time; time.sleep(30)"],
                                tmp_path, start + 0.15)
    assert result.timed_out and not result.ok
    assert time.monotonic() - start < 4


def test_probe_output_overflow_is_failure(tmp_path, install):
    import sys
    import time
    result = install._run_probe(tmp_path, [sys.executable, "-c", "print('x' * 70000)"],
                                tmp_path, time.monotonic() + 5)
    assert not result.ok and result.exit_code == -1
    assert len(result.stdout.encode()) <= install.OUTPUT_BYTES


def test_duplicate_probe_fields_are_not_accepted(install):
    assert install._object('{"status":"failed","status":"ok"}') == {}


@pytest.fixture
def runtime_probe(tmp_path, monkeypatch):
    import importlib.util
    import shutil
    import pbatch
    from pbatch import hub_web_assets
    root = Path(health.__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("installation_probe", root / "scripts/check-installation.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    package = tmp_path / "package"
    package.mkdir()
    for name in ("README.md", "role_keywords.yaml", "task_keywords.yaml"):
        shutil.copy2(root / "pbatch" / name, package / name)
    for name in ("hub_web_assets", "pwa_assets"):
        shutil.copytree(root / "pbatch" / name, package / name)
    monkeypatch.setattr(pbatch, "__file__", str(package / "__init__.py"))
    monkeypatch.setattr(hub_web_assets, "ASSET_ROOT", package / "hub_web_assets")
    return module, package


@pytest.mark.parametrize("damaged", ["role_keywords.yaml", "task_keywords.yaml", "pwa_assets/sw.js", "hub_web_assets/app.js"])
def test_runtime_probe_rejects_damaged_package_assets(runtime_probe, damaged):
    module, package = runtime_probe
    (package / damaged).write_bytes(b"[" if damaged.endswith("yaml") else b"")
    with pytest.raises(ValueError):
        module.main(["--json"])


def test_runtime_probe_rejects_missing_core_callable(runtime_probe, monkeypatch):
    from pbatch import tui
    module, _ = runtime_probe
    monkeypatch.setattr(tui, "tui_main", None)
    with pytest.raises(ValueError, match="required callable"):
        module.main(["--json"])


def test_unlaunchable_bundled_probe_is_structured_failure(monkeypatch, install):
    def unavailable(*_):
        raise ValueError("missing frozen script")
    monkeypatch.setattr(install, "script_argv", unavailable)
    report = install.installation_report(health.ASSET_ROOT)
    assert report["healthy"] is False
    assert report["checks"]["runtime"]["result"] == "failed"
    assert report["checks"]["knowledge_selfcheck"]["result"] == "failed"


def test_checker_spawning_uses_frozen_launcher(monkeypatch, install):
    from pbatch import launcher
    monkeypatch.setattr(launcher, "is_frozen", lambda: True)
    monkeypatch.setattr(launcher, "bundle_root", lambda: Path(health.ASSET_ROOT))
    calls = []
    from pbatch.process_exec import ValidationResult
    monkeypatch.setattr(install, "_run_probe", lambda _, args, *_rest: calls.append(args)
                        or ValidationResult(True, 0, "selfcheck: OK\n", "", False))
    assert install._selfcheck_probe(health.ASSET_ROOT, "knowledge", 1000000)["result"] == "passed"
    assert calls[0][1:] == [launcher.SCRIPT_FLAG, "scripts/check-knowledge-freshness.py", "--selfcheck"]


@pytest.mark.parametrize("values", [[], ["broken:main"], ["pbatch.cli:main", "pbatch.cli:main"]])
def test_runtime_probe_rejects_missing_wrong_or_duplicate_entrypoints(monkeypatch, runtime_probe, install, values):
    import importlib.metadata
    module, _ = runtime_probe
    monkeypatch.setattr(install, "installation_kind", lambda: "wheel")
    rows = [SimpleNamespace(name="abr", group="console_scripts", value=value) for value in values]
    rows.append(SimpleNamespace(name="pi-batch", group="console_scripts", value="pbatch.cli:main"))
    monkeypatch.setattr(importlib.metadata, "distribution", lambda _: SimpleNamespace(entry_points=rows))
    with pytest.raises(ValueError, match="entrypoints"):
        module._verify_entrypoints()
