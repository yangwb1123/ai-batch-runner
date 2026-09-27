"""Configuration trees stay bounded and diagnosable before normal execution."""

import json
import os
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
if os.environ['SAFETY_FROZEN'] == '1':
    sys.frozen = True
    sys._MEIPASS = os.environ['SAFETY_BUNDLE']
def reject_spawn(event, arguments):
    if event == 'subprocess.Popen':
        Path(os.environ['SAFETY_SPAWN']).write_text('unexpected subprocess')
        raise AssertionError('configuration safety must precede provider launch')
sys.addaudithook(reject_spawn)
from pbatch.cli import main
main()
"""
COMMANDS = (("config", "check", "--json"), ("doctor", "--json"),
            ("-p", "doctor", "--dry-run", "--memory-mode", "off"))
SECRET_SENTINEL = "secret-config-value-do-not-echo"
INVALID_TREES = [
    pytest.param("agent: &loop {agents: *loop}\n", id="mapping-cycle"),
    pytest.param("agent: {extra_args: &loop [*loop]}\n", id="list-cycle"),
    pytest.param("agent: {default_workers: .inf}\n", id="positive-infinity"),
    pytest.param("agent: {custom: -.inf}\n", id="negative-infinity"),
    pytest.param("agent: {custom: .nan}\n", id="nan"),
    pytest.param("agent: {custom: 1.0e+999}\n", id="float-overflow"),
    pytest.param("agent: {42: value}\n", id="integer-key"),
    pytest.param("agent: {true: value}\n", id="boolean-key"),
    pytest.param("agent: {null: value}\n", id="null-key"),
    pytest.param("agent: {custom: 2026-09-22}\n", id="date"),
    pytest.param("agent: {custom: 2026-09-22T10:30:00Z}\n", id="datetime"),
    pytest.param("agent: {custom: !!binary YWJj}\n", id="binary"),
    pytest.param("agent: {custom: !!set {one: null}}\n", id="set"),
    pytest.param("agent: {custom: !!pairs [{one: two}]}\n", id="pairs"),
    pytest.param('agent: {custom: "\\uD800"}\n', id="lone-surrogate"),
    *[pytest.param(f"agent: {{custom: !!{tag} {SECRET_SENTINEL}}}\n",
                   id=f"invalid-{tag}-literal") for tag in ("int", "float", "bool")],
]


@pytest.fixture
def configuration_tree(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bundle = tmp_path / "bundle"
    defaults = bundle / "share" / "ai-batch-runner"
    defaults.mkdir(parents=True)
    settings = tmp_path / "user" / ".config" / "settings.yaml"
    settings.parent.mkdir(parents=True)
    paths = {"project": workspace / "pi-batch.yaml", "user": settings,
             "defaults": defaults / "pi-batch.yaml"}
    paths["defaults"].write_text(
        "config_version: 1\nagent:\n  bin: " + json.dumps(sys.executable) + "\n",
        encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PBATCH_", "XDG_"))}
    env.update(PBATCH_HOME=str(settings.parent.parent),
               PBATCH_RUNTIME_HOME=str(tmp_path / "runtime"),
               PBATCH_TOOL_ROOT=str(defaults), PBATCH_SCRIPT_DIR=str(bundle),
               HOME=str(tmp_path / "home"), USERPROFILE=str(tmp_path / "home"),
               PYTHONPATH=str(ROOT), PATH="", SAFETY_FROZEN="1",
               SAFETY_BUNDLE=str(bundle), SAFETY_SPAWN=str(tmp_path / "spawn"))
    return {"workspace": workspace, "paths": paths, "env": env}


def _run(configuration, command, *, code=BOOTSTRAP, stdlib=False):
    arguments = [sys.executable, *(["-S"] if stdlib else []), "-c", code, *command]
    return subprocess.run(arguments, cwd=configuration["workspace"],
                          env=configuration["env"], capture_output=True, text=True,
                          encoding="utf-8", timeout=15)


def _assert_rejected(configuration, result, command, source="user"):
    assert "Traceback" not in result.stderr, result.stderr
    assert SECRET_SENTINEL not in result.stdout + result.stderr
    assert not Path(configuration["env"]["SAFETY_SPAWN"]).exists()
    assert len(result.stdout) + len(result.stderr) < 20000
    if command[0] == "-p":
        assert result.returncode == 2, result.stderr
        assert "Configuration error:" in result.stderr
        assert configuration["paths"][source].name in result.stderr
        assert not Path(configuration["env"]["PBATCH_RUNTIME_HOME"]).exists()
        return
    assert result.returncode == 1, result.stderr
    report = json.loads(result.stdout)
    if command[0] == "config":
        assert not report["valid"]
        record = next(item for item in report["sources"] if item["name"] == source)
        assert not record["loaded"] and record["errors"]
        assert report["validation"]["errors"]
    else:
        labels = {"user": "用户配置", "project": "项目配置", "defaults": "打包默认配置"}
        record = next(item for item in report if item["name"] == labels[source])
        assert not record["ok"] and record["detail"]


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("document", INVALID_TREES)
def test_invalid_tree_is_rejected_before_runtime(configuration_tree, command, document):
    configuration_tree["paths"]["user"].write_text(
        "config_version: 1\n" + document, encoding="utf-8")
    _assert_rejected(configuration_tree, _run(configuration_tree, command), command)


@pytest.mark.parametrize("command", COMMANDS)
def test_hex_integer_rejected_within_literal_limit(configuration_tree, command):
    literal = "0x" + "f" * 4094
    assert len(literal) == 4096
    configuration_tree["paths"]["user"].write_text(
        "config_version: 1\nagent: {custom: " + literal + "}\n", encoding="utf-8")
    result = _run(configuration_tree, command)
    _assert_rejected(configuration_tree, result, command)
    assert "integer exceeds supported size" in result.stdout + result.stderr


@pytest.mark.parametrize("source", ["project", "defaults"])
@pytest.mark.parametrize("command", COMMANDS)
def test_every_configuration_layer_checks_tree(configuration_tree, source, command):
    configuration_tree["paths"][source].write_text(
        "config_version: 1\nagent: &loop {agents: *loop}\n", encoding="utf-8")
    _assert_rejected(configuration_tree, _run(configuration_tree, command),
                     command, source)


def _alias_expansion_document(*, merge=False):
    if merge:
        lines = ["config_version: 1", "agent:", "  level0: &level0 {leaf: value}"]
        for index in range(1, 19):
            lines.append(f"  level{index}: &level{index} "
                         f"{{<<: [*level{index - 1}, *level{index - 1}]}}")
        return "\n".join(lines) + "\n"
    lines = ["config_version: 1", "agent:", "  level0: &level0 [leaf, leaf]"]
    for index in range(1, 19):
        lines.append(f"  level{index}: &level{index} "
                     f"[*level{index - 1}, *level{index - 1}]")
    return "\n".join(lines) + "\n"


def _alias_bytes_document():
    lines = ["agent:", "  level0: &level0 " + "x" * 9000]
    for index in range(1, 11):
        lines.append(f"  level{index}: &level{index} "
                     f"[*level{index - 1}, *level{index - 1}]")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize("command", COMMANDS)
@pytest.mark.parametrize("document", [
    pytest.param(_alias_expansion_document(), id="alias-expansion"),
    pytest.param(_alias_expansion_document(merge=True), id="merge-alias-expansion"),
    pytest.param(_alias_bytes_document(), id="alias-byte-expansion"),
    pytest.param("agent: {custom: " + "[" * 96 + "null" + "]" * 96 + "}\n",
                 id="deep-tree"),
    pytest.param("agent: {custom: " + "9" * 5000 + "}\n", id="long-integer"),
])
def test_expanded_tree_work_is_bounded(configuration_tree, command, document):
    configuration_tree["paths"]["user"].write_text(document, encoding="utf-8")
    _assert_rejected(configuration_tree, _run(configuration_tree, command), command)


@pytest.mark.parametrize("frozen", ["0", "1"])
def test_shared_alias_and_json_values_remain_supported(configuration_tree, frozen):
    configuration_tree["env"]["SAFETY_FROZEN"] = frozen
    configuration_tree["paths"]["user"].write_text(
        "config_version: 1\nagent:\n  default_workers: 2\n  agents:\n"
        "    first: &shared {default_model: demo, extra_args: [--safe]}\n"
        "    second: *shared\n"
        "    third: {<<: *shared, default_model: overridden}\n"
        "  custom: {nullable: null, boolean: true, integer: 7, finite: 0.125, "
        "text: '2026-09-22', sequence: [false, 0, plain]}\n", encoding="utf-8")
    result = _run(configuration_tree, ("config", "explain", "--json"))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "ok"
    agent = report["effective_file_config"]["agent"]
    assert agent["agents"]["first"] == agent["agents"]["second"] == {
        "default_model": "demo", "extra_args": ["--safe"]}
    assert agent["agents"]["third"] == {
        "default_model": "overridden", "extra_args": ["--safe"]}
    assert agent["custom"] == {"nullable": None, "boolean": True, "integer": 7,
                               "finite": 0.125, "text": "2026-09-22",
                               "sequence": [False, 0, "plain"]}
    assert report["resolved_runtime"]["agent.default_workers"] == 2
    for command in (COMMANDS[1], COMMANDS[2]):
        result = _run(configuration_tree, command)
        assert result.returncode == 0, result.stderr
        assert "Traceback" not in result.stderr
    assert not Path(configuration_tree["env"]["SAFETY_SPAWN"]).exists()


def test_bad_ambient_tree_does_not_prevent_explicit_file_check(configuration_tree):
    configuration_tree["paths"]["user"].write_text(
        "agent: &loop {agents: *loop}\n", encoding="utf-8")
    explicit = configuration_tree["workspace"] / "valid.yaml"
    explicit.write_text("config_version: 1\nagent: {}\n", encoding="utf-8")
    result = _run(configuration_tree, ("config", "check", "--file", str(explicit),
                                       "--json"))
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["valid"]
    assert not Path(configuration_tree["env"]["SAFETY_SPAWN"]).exists()


@pytest.mark.parametrize("size,limit", [(2 * 1024 * 1024 + 1, 4 * 1024 * 1024),
                                       (100, 64)])
def test_explicit_read_limit_only_tightens_hard_cap(configuration_tree, size, limit):
    path = configuration_tree["workspace"] / "sized.yaml"
    prefix = "config_version: 1\nagent: {}\n#"
    path.write_text(prefix + "x" * (size - len(prefix)), encoding="utf-8")
    result = _run(configuration_tree, ("config", "check", "--file", str(path),
                                       "--max-bytes", str(limit), "--json"))
    assert result.returncode == 1, result.stderr
    assert "Traceback" not in result.stderr
    report = json.loads(result.stdout)
    assert not report["valid"]
    assert report["sources"][0]["errors"]
    assert not report["sources"][0]["loaded"]


def test_stdlib_only_import_keeps_optional_yaml_compatibility(configuration_tree):
    for path in configuration_tree["paths"].values():
        path.write_text("agent: &loop {agents: *loop}\n", encoding="utf-8")
    code = ("from pbatch import config; assert config.yaml is None; "
            "assert config._BATCH_CFG == {}; assert config.CONFIG_ERRORS == (); "
            "assert config._find_user_config() == {}; "
            "assert config._find_batch_config() is None")
    result = _run(configuration_tree, (), code=code, stdlib=True)
    assert result.returncode == 0, result.stderr
    assert not Path(configuration_tree["env"]["PBATCH_RUNTIME_HOME"]).exists()
