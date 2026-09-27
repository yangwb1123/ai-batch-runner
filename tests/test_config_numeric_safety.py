"""Numeric configuration fallbacks must remain finite and diagnosable."""

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pbatch.config_values import _float_setting, _int_setting


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value, expected", [(7, 7), (7.9, 7), ("7", 7), (0, 0)])
def test_finite_integer_values_preserve_existing_conversion(value, expected, caplog):
    assert _int_setting({"count": value}, "count", 4, 0) == expected
    assert not caplog.records


@pytest.mark.parametrize("value", [True, False, math.inf, -math.inf, math.nan,
                                  "inf", "-inf", "nan", "1e999", sys.maxsize,
                                  -sys.maxsize, 10 ** 400, str(10 ** 400)])
def test_invalid_integer_values_warn_and_use_default(value, caplog):
    assert _int_setting({"count": value}, "count", 4, 0) == 4
    assert "Config value 'count' must be an integer; using 4" in caplog.text


@pytest.mark.parametrize("value", [sys.maxsize - 1, 1 - sys.maxsize])
def test_representable_integer_boundary_remains_supported(value, caplog):
    assert _int_setting({"count": value}, "count", 4) == value
    assert not caplog.records


@pytest.mark.parametrize("value, expected", [(0, 0.0), (1, 1.0), (0.25, 0.25),
                                            ("0.75", 0.75)])
def test_finite_float_values_preserve_existing_conversion(value, expected, caplog):
    assert _float_setting({"ratio": value}, "ratio", 0.5, 0.0, 1.0) == expected
    assert not caplog.records


@pytest.mark.parametrize("value", [True, False, math.inf, -math.inf, math.nan,
                                  "inf", "-Infinity", " +NaN ", "1e999",
                                  10 ** 400])
def test_invalid_float_values_warn_and_use_finite_default(value, caplog):
    resolved = _float_setting({"ratio": value}, "ratio", 0.5, 0.0, 1.0)
    assert resolved == 0.5 and math.isfinite(resolved)
    assert "Config value 'ratio' must be a number; using 0.5" in caplog.text


@pytest.mark.parametrize("value", [-0.25, 1.25])
def test_float_range_validation_still_warns_and_uses_default(value, caplog):
    assert _float_setting({"ratio": value}, "ratio", 0.5, 0.0, 1.0) == 0.5
    assert "must be within [0.0, 1.0]; using 0.5" in caplog.text


def _reject_nonfinite_json(value):
    raise ValueError(f"nonstandard JSON constant: {value}")


def _run_with_user_config(tmp_path, document, *arguments):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user = tmp_path / "user"
    (user / ".config").mkdir(parents=True)
    (user / ".config" / "settings.yaml").write_text(document, encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PBATCH_", "XDG_"))}
    env.update(PBATCH_HOME=str(user), PBATCH_SCRIPT_DIR=str(workspace),
               PBATCH_RUNTIME_HOME=str(tmp_path / "runtime"),
               HOME=str(tmp_path / "home"), USERPROFILE=str(tmp_path / "home"),
               PYTHONPATH=str(ROOT), PATH="")
    return subprocess.run(
        [sys.executable, "-m", "pbatch", *arguments],
        cwd=workspace, env=env, capture_output=True, text=True, encoding="utf-8",
        timeout=30)


def test_config_explain_keeps_quoted_nonfinite_values_out_of_runtime_json(tmp_path):
    result = _run_with_user_config(
        tmp_path, 'config_version: 1\nclassifier:\n  frontend_ratio: "nan"\n'
        '  backend_ratio: "inf"\nlimits:\n  per_second: "1e999"\n'
        '  burst: "-inf"\nagent:\n  default_workers: "inf"\n',
        "config", "explain", "--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout, parse_constant=_reject_nonfinite_json)
    runtime = report["resolved_runtime"]
    assert runtime["classifier.frontend_ratio"] == 0.5
    assert runtime["classifier.backend_ratio"] == 0.5
    assert runtime["limits.per_second"] == 0.0
    assert runtime["limits.burst"] == 1.0
    assert runtime["agent.default_workers"] == 4
    assert report["effective_file_config"]["classifier"]["frontend_ratio"] == "nan"
    assert "Config value 'frontend_ratio' must be a number; using 0.5" in result.stderr
    assert "Traceback" not in result.stderr


def test_huge_input_limit_does_not_break_doctor_imports(tmp_path):
    result = _run_with_user_config(
        tmp_path, "config_version: 1\ninput: {max_bytes: " + "9" * 100 + "}\n",
        "doctor", "--json")
    assert result.returncode == 1  # The isolated PATH deliberately has no provider.
    assert "Traceback" not in result.stderr
    report = json.loads(result.stdout, parse_constant=_reject_nonfinite_json)
    user = next(item for item in report if item["name"] == "用户配置")
    assert user["ok"]
    assert "Config value 'max_bytes' must be an integer; using 2097152" in result.stderr
