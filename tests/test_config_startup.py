"""First-use configuration contracts, including a simulated frozen bundle."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = """
import os
import sys
from pathlib import Path
sys.argv[0] = 'pi-batch'
if os.environ.get('TEST_FROZEN') == '1':
    sys.frozen = True
    sys._MEIPASS = os.environ['TEST_BUNDLE']
def reject_provider(event, args):
    if event == 'subprocess.Popen':
        Path(os.environ['TEST_SPAWN_MARKER']).write_text(repr(args))
        raise AssertionError('diagnostics must not launch subprocesses')
sys.addaudithook(reject_provider)
from pbatch.cli import main
main()
"""


@pytest.fixture
def first_use(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundle = tmp_path / "bundle"
    defaults = bundle / "share" / "ai-batch-runner"
    defaults.mkdir(parents=True)
    (defaults / "pi-batch.yaml").write_text(
        "config_version: 1\nagent:\n  bin: packaged-provider\n"
        "  default_timeout: 1500\n  default_workers: 3\n"
        "validators:\n  packaged: echo packaged\n", encoding="utf-8")
    settings = tmp_path / "user" / ".config" / "settings.yaml"
    settings.parent.mkdir(parents=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PBATCH_", "XDG_"))}
    env.update(PBATCH_HOME=str(settings.parent.parent),
               PBATCH_RUNTIME_HOME=str(tmp_path / "runtime"),
               PBATCH_TOOL_ROOT=str(defaults), PBATCH_SCRIPT_DIR=str(bundle),
               HOME=str(tmp_path / "home"), USERPROFILE=str(tmp_path / "home"),
               PYTHONPATH=str(ROOT), PATH="", TEST_FROZEN="1",
               TEST_BUNDLE=str(bundle), TEST_SPAWN_MARKER=str(tmp_path / "spawn"))
    return workspace, settings, env


def _run(first_use, *arguments):
    workspace, _, env = first_use
    return subprocess.run([sys.executable, "-c", BOOTSTRAP, *arguments],
                          cwd=workspace, env=env, capture_output=True,
                          text=True, encoding="utf-8", timeout=30)


def _assert_no_spawn(first_use, result):
    assert "Traceback" not in result.stderr, result.stderr
    assert not Path(first_use[2]["TEST_SPAWN_MARKER"]).exists()


@pytest.mark.parametrize("frozen", ["0", "1"])
def test_user_overrides_packaged_defaults(first_use, frozen):
    _, settings, env = first_use
    env["TEST_FROZEN"] = frozen
    settings.write_text("config_version: 1\nagent:\n  bin: user-provider\n"
                        "  default_timeout: 37\n", encoding="utf-8")
    result = _run(first_use, "config", "explain", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["resolved_runtime"]["agent.bin"] == "user-provider"
    assert report["resolved_runtime"]["agent.default_timeout"] == 37
    assert report["resolved_runtime"]["agent.default_workers"] == 3
    assert "packaged" in report["resolved_runtime"]["validators.names"]
    sources = {source["name"]: source for source in report["sources"]}
    assert not sources["project"]["present"]
    assert sources["defaults"]["path"] == str(
        Path(env["PBATCH_TOOL_ROOT"]) / "pi-batch.yaml")
    provenance = report["provenance"]["agent"]["children"]
    assert provenance["bin"]["source"] == "user"
    assert provenance["default_workers"]["source"] == "defaults"
    _assert_no_spawn(first_use, result)


def test_explicit_project_overrides_user_preserving_other_defaults(first_use):
    workspace, settings, _ = first_use
    settings.write_text("config_version: 1\nagent:\n  bin: user-provider\n"
                        "  default_timeout: 37\n", encoding="utf-8")
    (workspace / "pi-batch.yaml").write_text(
        "config_version: 1\nagent:\n  bin: project-provider\n", encoding="utf-8")
    result = _run(first_use, "config", "explain", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["resolved_runtime"]["agent.bin"] == "project-provider"
    assert report["resolved_runtime"]["agent.default_timeout"] == 37
    assert report["resolved_runtime"]["agent.default_workers"] == 3
    assert report["provenance"]["agent"]["children"]["bin"]["source"] == "project"


def test_source_shim_adjacent_config_still_precedes_cwd(first_use):
    workspace, settings, env = first_use
    env["TEST_FROZEN"] = "0"
    (Path(env["PBATCH_SCRIPT_DIR"]) / "pi-batch.yaml").write_text(
        "config_version: 1\nagent:\n  bin: shim-provider\n", encoding="utf-8")
    (workspace / "pi-batch.yaml").write_text(
        "config_version: 1\nagent:\n  bin: cwd-provider\n", encoding="utf-8")
    settings.write_text("config_version: 1\nagent:\n  bin: user-provider\n",
                        encoding="utf-8")
    result = _run(first_use, "config", "explain", "--json")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["resolved_runtime"]["agent.bin"] == "shim-provider"


@pytest.mark.parametrize("source", ["user", "project", "defaults"])
@pytest.mark.parametrize("bad_yaml", ["agent: [\n", "- invalid\n", "false\n",
                                     "config_version: 999\n", b"agent: \xff\n"])
@pytest.mark.parametrize("command", [("config", "check"), ("doctor",)])
def test_bad_config_is_diagnosable_without_traceback(first_use, source, bad_yaml, command):
    workspace, settings, env = first_use
    paths = {"user": settings, "project": workspace / "pi-batch.yaml",
             "defaults": Path(env["PBATCH_TOOL_ROOT"]) / "pi-batch.yaml"}
    raw = bad_yaml if isinstance(bad_yaml, bytes) else bad_yaml.encode("utf-8")
    paths[source].write_bytes(raw)
    result = _run(first_use, *command, "--json")
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    if command[0] == "config":
        assert not report["valid"]
        assert report["validation"]["errors"]
    else:
        label = {"user": "用户配置", "project": "项目配置", "defaults": "打包默认配置"}
        check = next(item for item in report if item["name"] == label[source])
        assert not check["ok"] and check["detail"]
    _assert_no_spawn(first_use, result)


@pytest.mark.parametrize("source", ["user", "project"])
@pytest.mark.parametrize("command", [(), ("tui",), ("-p", "doctor"),
                                     ("-p", "config")])
def test_bad_config_blocks_normal_execution_before_provider(first_use, source, command):
    workspace, settings, _ = first_use
    path = settings if source == "user" else workspace / "pi-batch.yaml"
    path.write_text("agent: [\n", encoding="utf-8")
    result = _run(first_use, *command)
    assert result.returncode == 2
    assert "Configuration error:" in result.stderr
    assert f"{path.name}: invalid YAML" in result.stderr
    assert "line 2, column 1" in result.stderr
    _assert_no_spawn(first_use, result)


def test_explicit_inspection_isolated_from_bad_ambient_config(first_use):
    workspace, settings, _ = first_use
    settings.write_text("agent: [\n", encoding="utf-8")
    (workspace / "pi-batch.yaml").write_text("agent: [\n", encoding="utf-8")
    valid = workspace / "valid.yaml"
    valid.write_text("config_version: 1\nagent:\n  bin: explicit\n", encoding="utf-8")
    result = _run(first_use, "config", "check", "--file", str(valid), "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["valid"]
    assert not any(source["present"] for source in report["sources"][1:])
    _assert_no_spawn(first_use, result)


def test_parser_diagnostics_do_not_echo_secrets(first_use):
    _, settings, _ = first_use
    settings.write_text('agent:\n  api_token: "do-not-print-secret\n', encoding="utf-8")
    result = _run(first_use, "config", "check", "--json")
    assert result.returncode == 1
    assert "invalid YAML" in result.stdout
    assert "do-not-print-secret" not in result.stdout + result.stderr
    _assert_no_spawn(first_use, result)


def test_deep_yaml_has_bounded_diagnostic(first_use):
    _, settings, _ = first_use
    settings.write_text("agent: " + "[" * 1500 + "]" * 1500, encoding="utf-8")
    result = _run(first_use, "config", "check", "--json")
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert any("YAML depth exceeds limit" in error
               for error in report["validation"]["errors"])
    assert len(result.stdout) < 4000
    _assert_no_spawn(first_use, result)


def test_provenance_respects_nested_mapping_replacement():
    from pbatch.config import _merge_config
    from pbatch.config_inspect import _source_tree
    project = {"agent": {"agents": {"one": {"model": "project"}}}}
    user = {"agent": {"agents": {"two": {"model": "user"}}}}
    defaults = {"agent": {"agents": {"three": {"model": "default"}}}}
    effective = _merge_config(project, _merge_config(user, defaults))
    provenance = _source_tree(project, user, defaults)
    children = provenance["agent"]["children"]["agents"]["children"]
    assert set(children) == set(effective["agent"]["agents"]) == {"one"}
    assert children["one"]["children"]["model"]["source"] == "project"


def test_stdlib_only_copied_cli_ignores_unreadable_yaml_layers(first_use, tmp_path):
    workspace, settings, env = first_use
    copied = tmp_path / "copied"
    shutil.copytree(ROOT / "pbatch", copied / "pbatch",
                    ignore=shutil.ignore_patterns("__pycache__"))
    settings.write_text("agent: [\n", encoding="utf-8")
    (workspace / "pi-batch.yaml").write_text("agent: [\n", encoding="utf-8")
    (Path(env["PBATCH_TOOL_ROOT"]) / "pi-batch.yaml").write_text(
        "agent: [\n", encoding="utf-8")
    env["PYTHONPATH"] = str(copied)
    probe = subprocess.run(
        [sys.executable, "-S", "-c", "from pbatch import config; "
         "assert config.yaml is None; assert config._BATCH_CFG == {}; "
         "assert config._find_batch_config() is None; "
         "assert config._find_user_config() == {}"],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
    assert probe.returncode == 0, probe.stderr
    schema = workspace / "schema.json"
    schema.write_text('{"type": "integer", "minimum": 1}', encoding="utf-8")
    artifact = workspace / "artifact.json"
    artifact.write_text("2", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-S", str(copied / "pbatch" / "json_schema_cli.py"),
         "--schema", str(schema), str(artifact)],
        cwd=workspace, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "pass"
    assert "JSONSCHEMA: OK" in result.stderr
    _assert_no_spawn(first_use, result)
