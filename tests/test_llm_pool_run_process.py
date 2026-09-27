"""Real POSIX exec smoke tests, using only local Python/fake executables."""

from copy import deepcopy
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys

import pytest

from pbatch.llm_pool_config import load_config
from pbatch.llm_pool_run import INLINE_ENV
from test_llm_pool_config import pool_file


pytestmark = pytest.mark.scenario
ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "pi-batch.py"


def command(path, *child):
    return [sys.executable, str(CLI), "pool", "--config", str(path), "run",
            "--model", "glm-5.1", "--", *child]


def test_real_child_receives_merged_environment_exact_argv_stdio_and_cwd(pool_file, tmp_path):
    path, _ = pool_file
    secret = "fake-other-provider-secret-123456789"
    inline = {"provider": {"other": {"options": {"apiKey": secret}}}, "permission": {"bash": "ask"}}
    environment = {**os.environ, INLINE_ENV: json.dumps(inline), "KEEP_VALUE": "kept"}
    before = deepcopy(environment)
    argument = 'literal $(touch must-not-exist); "quotes" & *  中文'
    code = (
        "import json, os, sys\n"
        "config = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])\n"
        "assert config['model'] == config['small_model'] == 'opencode-go/glm-5.1'\n"
        "assert config['provider']['other']['options']['apiKey'] == sys.argv[2]\n"
        "assert config['permission'] == {'bash':'ask'}\n"
        "assert config['provider']['opencode-go']['options']['apiKey'].startswith('{file:/')\n"
        "assert os.environ['KEEP_VALUE'] == 'kept'\n"
        "assert sys.argv[3] == ''\n"
        "print(json.dumps({'argument':sys.argv[1], 'stdin':sys.stdin.read(), 'cwd':os.getcwd()}))\n"
        "print('child-only-stderr', file=sys.stderr)\n"
    )
    result = subprocess.run(command(path, sys.executable, "-c", code, argument, secret, ""),
                            env=environment, cwd=tmp_path, input="incoming 中文\n", text=True,
                            capture_output=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"argument": argument, "stdin": "incoming 中文\n", "cwd": str(tmp_path)}
    assert result.stderr == "child-only-stderr\n" and secret not in result.stdout + result.stderr
    assert environment == before and not (tmp_path / "must-not-exist").exists()
    assert not load_config(str(path)).token_file.exists()
    assert not load_config(str(path)).state_file.parent.exists()


@pytest.mark.parametrize("code", [0, 7, 42, 126, 127])
def test_child_exit_code_is_not_replaced_by_wrapper_status(pool_file, tmp_path, code):
    path, _ = pool_file
    result = subprocess.run(command(path, sys.executable, "-c", f"raise SystemExit({code})"),
                            cwd=tmp_path, capture_output=True, timeout=10, check=False)
    assert result.returncode == code
    assert result.stdout == result.stderr == b""


def test_signal_reaches_exec_replacement_without_orphan_child(pool_file, tmp_path):
    path, _ = pool_file
    code = "import os,time; print(os.getpid(), flush=True); time.sleep(30)"
    with subprocess.Popen(command(path, sys.executable, "-c", code), cwd=tmp_path,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        try:
            assert select.select([process.stdout], [], [], 10)[0], "fake child did not become ready"
            child_pid = int(process.stdout.readline().strip())
            assert child_pid == process.pid  # There is no intermediate supervisor to orphan it.
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == -signal.SIGTERM
            assert stdout == stderr == ""
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_path_resolution_and_executable_with_spaces(pool_file, tmp_path):
    path, _ = pool_file
    executable = tmp_path / "fake opencode"
    executable.write_text(f"#!{sys.executable}\nimport sys\nprint('path-resolved')\n", encoding="utf-8")
    executable.chmod(0o700)
    environment = {**os.environ, "PATH": str(tmp_path)}
    result = subprocess.run(command(path, executable.name), cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0 and result.stdout == "path-resolved\n"
    assert not result.stderr


@pytest.mark.parametrize("kind,code", [("missing", 127), ("not-executable", 126), ("no-shebang", 126)])
def test_exec_failure_has_no_shell_fallback_or_sensitive_argv_echo(pool_file, tmp_path, kind, code):
    path, _ = pool_file
    executable = tmp_path / "secret-command-name-123456789"
    marker = tmp_path / "must-not-execute"
    if kind != "missing":
        executable.write_text(f"touch '{marker}'\n", encoding="utf-8")
        executable.chmod(0o600 if kind == "not-executable" else 0o700)
    result = subprocess.run(command(path, str(executable), "secret-arg-123456789"), cwd=tmp_path,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == code and not result.stdout
    assert "secret-command" not in result.stderr and "secret-arg" not in result.stderr
    assert not marker.exists()


def test_dry_run_does_not_launch_and_flags_after_delimiter_remain_child_arguments(pool_file, tmp_path):
    path, _ = pool_file
    marker = tmp_path / "must-not-execute"
    args = [sys.executable, str(CLI), "pool", "--config", str(path), "run", "--model", "glm-5.1",
            "--dry-run", "--", sys.executable, "-c", f"open({str(marker)!r},'w').close()", "--model", "other"]
    result = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["model"] == "glm-5.1" and report["argument_count"] == 5
    assert not marker.exists() and not result.stderr
    assert "must-not-execute" not in result.stdout


def test_invalid_inherited_config_is_not_dropped_before_launch(pool_file, tmp_path):
    path, _ = pool_file
    marker = tmp_path / "must-not-execute"
    environment = {**os.environ, INLINE_ENV: '{"secret-config-fragment": [broken'}
    result = subprocess.run(command(path, sys.executable, "-c", f"open({str(marker)!r},'w').close()"),
                            env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 2 and not marker.exists()
    assert "secret-config-fragment" not in result.stdout + result.stderr


def test_help_does_not_require_configuration_or_credentials(tmp_path):
    result = subprocess.run([sys.executable, str(CLI), "pool", "run", "--help"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0 and "--small-model" in result.stdout
    assert "--dry-run" in result.stdout and not result.stderr
