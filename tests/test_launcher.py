"""Standalone child dispatch preserves arguments and confines checker execution."""

from __future__ import annotations

import shlex
import subprocess
import sys

import pytest

from pbatch import launcher
from pbatch.bundled_commands import embedded_checker_argv, embedded_checker_command
from pbatch.cmd_expand import expand_cmd
from pbatch.validator_schema import schema_validator_command


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    extraction = tmp_path / "extraction space"
    root = extraction / "share" / "ai-batch-runner"
    (root / "scripts").mkdir(parents=True)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(extraction), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "binary with spaces"))
    return root


def test_source_launcher_preserves_checkout_and_module_contract(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    entry = tmp_path / "pi-batch.py"
    entry.write_text("pass\n")
    assert launcher.cli_argv("tui", entrypoint=entry) == [sys.executable, str(entry), "tui"]
    assert launcher.cli_argv("rules", "--check") == [
        sys.executable, "-m", "pbatch", "rules", "--check"]
    assert launcher.script_argv(entry, "two words") == [sys.executable, str(entry), "two words"]


def test_frozen_checker_identifier_survives_new_extraction_root(bundle, tmp_path, monkeypatch, capsys):
    script = bundle / "scripts" / "check-demo.py"
    script.write_text("import sys\nprint(repr(sys.argv[1:]))\n")
    command = launcher.script_argv(script, "space and 'quote'", "--flag")
    assert command[:3] == [sys.executable, launcher.SCRIPT_FLAG, "scripts/check-demo.py"]
    new_extraction = tmp_path / "child extraction"
    relocated = new_extraction / "share" / "ai-batch-runner" / "scripts" / script.name
    relocated.parent.mkdir(parents=True)
    relocated.write_text(script.read_text())
    monkeypatch.setattr(sys, "_MEIPASS", str(new_extraction))
    assert launcher.dispatch_bundled_script(command[1:])
    assert "space and 'quote'" in capsys.readouterr().out
    assert launcher.cli_argv("eval", entrypoint=tmp_path / "wrong.py") == [sys.executable, "eval"]


def test_checker_dispatch_supports_sibling_imports_and_restores_process_state(bundle, capsys):
    (bundle / "scripts" / "_launcher_peer.py").write_text("VALUE = 'sibling imported'\n")
    (bundle / "scripts" / "check-demo.py").write_text(
        "from _launcher_peer import VALUE\nprint(VALUE)\nraise SystemExit(7)\n")
    previous_argv, previous_path = sys.argv[:], sys.path[:]
    try:
        with pytest.raises(SystemExit) as error:
            launcher.dispatch_bundled_script([launcher.SCRIPT_FLAG, "scripts/check-demo.py"])
        assert error.value.code == 7
        assert "sibling imported" in capsys.readouterr().out
        assert sys.argv == previous_argv and sys.path == previous_path
    finally:
        sys.modules.pop("_launcher_peer", None)


@pytest.mark.parametrize("identifier", ["../outside.py", "pbatch/cli.py", "missing.py"])
def test_dispatch_rejects_nonchecker_paths(bundle, identifier):
    with pytest.raises(SystemExit) as error:
        launcher.dispatch_bundled_script([launcher.SCRIPT_FLAG, identifier])
    assert error.value.code == 2


def test_tool_root_override_cannot_authorize_external_script(bundle, tmp_path, monkeypatch):
    external = tmp_path / "outside.py"
    external.write_text("raise AssertionError('must not execute')\n")
    monkeypatch.setenv("PBATCH_TOOL_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="outside"):
        launcher.script_argv(external)
    link = bundle / "scripts" / "link.py"
    try:
        link.symlink_to(external)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(SystemExit) as error:
        launcher.dispatch_bundled_script([launcher.SCRIPT_FLAG, str(link)])
    assert error.value.code == 2


def test_private_dispatch_requires_frozen_runtime(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert not launcher.dispatch_bundled_script(["rules", "--check"])
    with pytest.raises(SystemExit) as error:
        launcher.dispatch_bundled_script([launcher.SCRIPT_FLAG, "quality.py"])
    assert error.value.code == 2


def test_embedded_validator_preserves_shell_tail_and_quoted_paths(bundle):
    checker = bundle / "scripts" / "check-demo.py"
    checker.write_text("pass\n")
    command = f"python {shlex.quote(str(checker))} {{output}} && echo '$literal'"
    expanded = expand_cmd(command, "/tmp/quoted ' result.json", "/tmp")
    prefix = shlex.join(launcher.script_argv(checker))
    assert expanded == prefix + " '/tmp/quoted '\"'\"' result.json' && echo '$literal'"
    for project_command in ("python custom.py x", "python quality.py .",
                            "echo 'python custom.py'", "python '$SCRIPT'"):
        assert embedded_checker_command(project_command) == project_command


def test_frozen_jsonschema_uses_embedded_checker(bundle):
    (bundle / "scripts" / "check-json-schema.py").write_text("pass\n")
    command = schema_validator_command('jsonschema=schemas/result schema.json')
    assert shlex.split(command) == [sys.executable, launcher.SCRIPT_FLAG,
        "scripts/check-json-schema.py", "--schema", "schemas/result schema.json", "{output}"]


def test_registered_validator_quotes_extraction_root_at_execution(bundle, monkeypatch):
    from pbatch import assets
    monkeypatch.setattr(assets, "TOOL_ROOT", str(bundle))
    checker = bundle / "scripts" / "check-demo.py"
    checker.write_text("pass\n")
    raw = "python {tool_root}/scripts/check-demo.py {output}"
    registry = assets.expand_validator_registry({"demo": {"cmd": raw, "scope": "file"}},
                                                "src", ".", "reports")
    assert shlex.split(registry["demo"]["cmd"]) == ["python", str(checker), "{output}"]
    expanded = expand_cmd(registry["demo"]["cmd"], "/tmp/output space.json",
                          "/tmp", tool_root=str(bundle))
    assert shlex.split(expanded) == [sys.executable, launcher.SCRIPT_FLAG,
        "scripts/check-demo.py", "/tmp/output space.json"]


@pytest.mark.parametrize("executable", [
    "/opt/binary with 'quotes'/pi-batch", r"C:\Program Files\pi-batch.exe"])
def test_literal_checker_argv_preserves_paths_and_metacharacters(bundle, monkeypatch, executable):
    (bundle / "scripts" / "check-demo.py").write_text("pass\n")
    monkeypatch.setattr(sys, "executable", executable)
    expected = launcher.script_argv("scripts/check-demo.py", "", "a 'quoted' file.json",
                                   r"C:\Users\demo\result.json", "$HOME; echo unsafe", "*.py")
    assert embedded_checker_argv(shlex.join(expected)) == expected


@pytest.mark.parametrize("suffix", [
    " && echo done", " | cat", " > result.txt", " ; echo done", " &",
    " $HOME", " ${HOME}", " $(pwd)", " `pwd`", " *.py", " a?.py", " [ab].py",
    " ~/file", " {a,b}", " # comment", "\necho done", " \\\nfoo", ' "$HOME"'])
def test_literal_checker_leaves_shell_syntax_untouched(bundle, suffix):
    (bundle / "quality.py").write_text("pass\n")
    invocation = shlex.join(launcher.script_argv("quality.py"))
    assert embedded_checker_argv(invocation + suffix) is None


def test_literal_checker_requires_valid_private_dispatch(bundle, monkeypatch):
    (bundle / "quality.py").write_text("pass\n")
    invocation = launcher.script_argv("quality.py")
    for argv in (["python", "quality.py"], [*invocation[:2], "../outside.py"],
                 [*invocation[:2], "scripts/missing.py"], [sys.executable, "check"],
                 ["other-binary", *invocation[1:]]):
        assert embedded_checker_argv(shlex.join(argv)) is None
    monkeypatch.delattr(sys, "frozen")
    assert embedded_checker_argv(shlex.join(invocation)) is None


def test_literal_command_rejection_is_bounded_for_long_shell_commands():
    code = (
        "from pbatch.bundled_commands import _LITERAL_COMMAND, _PYTHON_SCRIPT\n"
        "command = '/tmp/' + 'a' * 4096 + ' --_bundled-script quality.py && echo done'\n"
        "assert _LITERAL_COMMAND.fullmatch(command) is None\n"
        "for suffix in (';echo done', '$SUFFIX'):\n"
        " assert _PYTHON_SCRIPT.match('python /tmp/' + 'a' * 4096 + suffix) is None\n")
    subprocess.run([sys.executable, "-c", code], check=True, timeout=3,
                   capture_output=True, text=True)
