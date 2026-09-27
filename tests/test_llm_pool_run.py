"""Bounded inline config merging and secret-free, zero-side-effect launch plans."""

from argparse import Namespace
from copy import deepcopy
from dataclasses import replace
import errno
import json
import os
from pathlib import Path

import pytest

from pbatch.llm_pool_cli import pool_main
from pbatch.llm_pool_client import client_config
from pbatch.llm_pool_config import PoolConfigCommandError, load_config
from pbatch.llm_pool_run import INLINE_ENV, merged_client_config, prepare_launch
from test_llm_pool_config import pool_file


def launch_args(**overrides):
    return Namespace(**{"model": "glm-5.1", "small_model": "", "dry_run": True,
                        "argv": ["--", "fake-opencode", "run", "private prompt"], **overrides})


def merge(settings, data, *, small=""):
    return json.loads(merged_client_config(settings, "glm-5.1", small, json.dumps(data)))


def test_merge_preserves_unrelated_inline_config_without_mutating_environment(pool_file):
    path, _ = pool_file
    settings = load_config(str(path))
    original = {"model": "other/old-model", "small_model": "other/old-small",
                "permission": {"bash": "ask"}, "mcp": {"example": {"enabled": False}},
                "agent": {"review": {"model": "other/review-model"}}, "instructions": ["guide.md"],
                "provider": {"other": {"options": {"apiKey": "test-inherited-secret-123456789"}},
                             "opencode-go": {"name": "My pool", "options": {"timeout": 1234,
                                              "baseURL": "https://old.invalid", "apiKey": "old-key"},
                                              "models": {"glm-5.1": {"name": "My model"}}}}}
    environment = {INLINE_ENV: json.dumps(original), "PATH": "/bin", "EXISTING": "unchanged"}
    before = deepcopy(environment)
    command, child, report = prepare_launch(settings, launch_args(), environment)
    result = json.loads(child[INLINE_ENV])
    assert environment == before and command == ["fake-opencode", "run", "private prompt"]
    assert child is not environment and child["EXISTING"] == "unchanged"
    for name in ("permission", "mcp", "agent", "instructions"):
        assert result[name] == original[name]
    assert result["provider"]["other"] == original["provider"]["other"]
    assert result["model"] == result["small_model"] == "opencode-go/glm-5.1"
    provider = result["provider"]["opencode-go"]
    assert provider["name"] == "My pool" and provider["models"] == original["provider"]["opencode-go"]["models"]
    assert provider["options"] == {"timeout": 1234, "baseURL": "http://127.0.0.1:8741/v1",
                                    "apiKey": "{file:" + str(settings.token_file) + "}"}
    assert report["inline_config_merged"] is True
    assert "test-inherited-secret-123456789" not in json.dumps(report)
    assert "private prompt" not in json.dumps(report)


@pytest.mark.parametrize("raw", ["", "  \n ", "{}"])
def test_empty_inline_config_and_explicit_small_model(pool_file, raw):
    path, _ = pool_file
    settings = load_config(str(path))
    result = json.loads(merged_client_config(settings, "glm-5.1", "minimax-m2.7", raw))
    assert result["model"] == "opencode-go/glm-5.1"
    assert result["small_model"] == "opencode-go/minimax-m2.7"
    assert result["provider"] == client_config(settings, "glm-5.1")["provider"]
    assert not settings.token_file.exists() and not settings.state_file.exists()


@pytest.mark.parametrize("raw", [
    "[]", "null", "1", '"string"', "{", "{\"a\":1,}", "{/*comment*/}",
    '{"a":1,"a":2}', '{"nested":{"a":1,"a":2}}', '{"a":NaN}', '{"a":Infinity}',
    '{"a":-Infinity}', '{"a":1e999}', '{"a":[' + "[" * 40 + "0" + "]" * 40 + "]}",
    "[" * 2000 + "]" * 2000, '{"text":"' + "x" * 65536 + '"}',
    '{"text":"' + "字" * 23000 + '"}', '{"a":"\\ud800"}', "\ud800", "\ufeff{}",
])
def test_bad_or_unbounded_inline_config_fails_without_echoing_raw(pool_file, monkeypatch, capsys, raw):
    path, _ = pool_file
    # Inject through a copied mapping: the OS itself refuses some malformed environment strings.
    monkeypatch.setattr("pbatch.llm_pool_run.os.environ", {INLINE_ENV: raw})
    assert pool_main(["--config", str(path), "run", "--model", "glm-5.1", "--dry-run",
                      "--", "never-launch"]) == 2
    output = capsys.readouterr()
    assert not output.out and "Traceback" not in output.err
    assert len(output.err) < 300
    assert not load_config(str(path)).state_file.exists()


@pytest.mark.parametrize("data", [
    {"provider": []}, {"provider": {"opencode-go": None}},
    {"provider": {"opencode-go": {"options": []}}},
    {"provider": {"opencode-go": {"models": []}}},
    {"provider": {"opencode-go": {"models": {"glm-5.1": None}}}},
    {"enabled_providers": []}, {"enabled_providers": ["other"]},
    {"disabled_providers": ["opencode-go"]}, {"enabled_providers": None},
    {"disabled_providers": [True]}, {"disabled_providers": "opencode-go"},
    {"provider": {"opencode-go": {"whitelist": ["glm-5.1"]}}},
    {"provider": {"opencode-go": {"blacklist": ["minimax-m2.7"]}}},
    {"provider": {"opencode-go": {"blacklist": None}}},
])
def test_conflicting_filters_and_malformed_sections_are_not_overwritten(pool_file, data):
    path, _ = pool_file
    original = deepcopy(data)
    with pytest.raises(PoolConfigCommandError):
        merge(load_config(str(path)), data, small="minimax-m2.7")
    assert data == original


@pytest.mark.parametrize("provider", [
    {"npm": "custom-sdk"}, {"npm": None}, {"options": {"fetch": "custom"}},
    {"options": {"endpoint": "https://example.invalid"}},
    {"options": {"headers": {"Authorization": "secret-123456789"}}},
    {"options": {"headers": {"X-API-KEY": "secret-123456789"}}},
    {"options": {"headers": {"Cookie": "secret-123456789"}}},
    {"options": {"headers": {"Host": "remote.invalid"}}},
    {"options": {"headers": {"x-opencode-session": "constant-session"}}},
    {"options": {"headers": {"Proxy-Authorization": "secret-123456789"}}},
    {"options": {"headers": []}}, {"options": {"headers": {"X-Custom": 123}}},
    {"models": {"glm-5.1": {"provider": {"npm": "another-sdk"}}}},
    {"models": {"glm-5.1": {"id": "alias-model"}}},
    {"models": {"minimax-m2.7": {"headers": {"authorization": "secret-123456789"}}}},
])
def test_inline_transport_and_auth_conflicts_require_manual_review(pool_file, provider):
    path, _ = pool_file
    with pytest.raises(PoolConfigCommandError) as caught:
        merge(load_config(str(path)), {"provider": {"opencode-go": provider}}, small="minimax-m2.7")
    assert "secret-123456789" not in str(caught.value)
    assert "example.invalid" not in str(caught.value)


def test_compatible_filters_options_and_policies_are_preserved(pool_file):
    path, _ = pool_file
    data = {"enabled_providers": ["other", "opencode-go"], "disabled_providers": ["disabled"],
            "experimental": {"policies": [{"effect": "deny", "action": "provider.use", "resource": "other"}]},
            "provider": {"opencode-go": {"whitelist": ["glm-5.1", "minimax-m2.7"],
                         "blacklist": ["unused-model"], "options": {"headers": {"X-Custom": "kept"}},
                         "models": {"glm-5.1": {"id": "glm-5.1", "name": "display name"}}}}}
    result = merge(load_config(str(path)), data)
    for name in ("enabled_providers", "disabled_providers", "experimental"):
        assert result[name] == data[name]
    assert result["provider"]["opencode-go"]["options"]["headers"] == {"X-Custom": "kept"}


@pytest.mark.parametrize("arguments", [[], ["fake"], ["--"], ["--", ""], ["--", "-bad"],
                                     ["--", "fake\0command"], ["--", "fake", "arg\0value"]])
def test_command_requires_explicit_delimiter_and_valid_argv(pool_file, arguments):
    path, _ = pool_file
    with pytest.raises(PoolConfigCommandError):
        prepare_launch(load_config(str(path)), launch_args(argv=arguments), {})


@pytest.mark.parametrize("fields", [{"model": "unknown"}, {"small_model": "unknown"}, {"model": ""}])
def test_both_selected_models_must_be_configured(pool_file, fields):
    path, _ = pool_file
    with pytest.raises(PoolConfigCommandError):
        prepare_launch(load_config(str(path)), launch_args(**fields), {})


def test_bound_applies_to_merged_output_not_only_inherited_input(pool_file, monkeypatch):
    path, _ = pool_file
    monkeypatch.setattr("pbatch.llm_pool_run.MAX_CONFIG_BYTES", 128)
    with pytest.raises(PoolConfigCommandError, match="merged inline configuration"):
        prepare_launch(load_config(str(path)), launch_args(), {})


@pytest.mark.parametrize("suffix", ['quote"', "brace}", "brace{", "back\\slash", "line\n",
                                  "line\r", "control\x01", "delete\x7f", "\ud800"])
def test_token_reference_rejects_interpolation_and_escaped_path_ambiguity(pool_file, suffix):
    path, _ = pool_file
    settings = replace(load_config(str(path)), token_file=path.parent / suffix)
    with pytest.raises(PoolConfigCommandError):
        client_config(settings, "glm-5.1")


def test_token_reference_supports_spaces_and_unicode_without_reading_file(pool_file):
    path, _ = pool_file
    settings = replace(load_config(str(path)), token_file=path.parent / "私有 密钥" / "网关令牌")
    merged = merged_client_config(settings, "glm-5.1", "", "")
    assert "私有 密钥" in merged and "\\u" not in merged
    assert json.loads(merged)["provider"]["opencode-go"]["options"]["apiKey"] == (
        "{file:" + str(settings.token_file) + "}")
    assert not settings.token_file.parent.exists()


def test_relative_token_reference_cannot_depend_on_child_working_directory(pool_file):
    path, _ = pool_file
    settings = replace(load_config(str(path)), token_file=Path("relative-token"))
    with pytest.raises(PoolConfigCommandError):
        client_config(settings, "glm-5.1")


def test_cli_preview_is_redacted_and_has_no_exec_network_key_or_state_access(pool_file, capsys, monkeypatch):
    path, _ = pool_file
    secret = "test-launch-secret-123456789"
    inherited = {"provider": {"private-provider-name": {"options": {"apiKey": secret}}}}
    monkeypatch.setenv(INLINE_ENV, json.dumps(inherited))
    def forbidden(*args, **kwargs):
        pytest.fail("preview must not execute or access credentials/network/state")
    monkeypatch.setattr("os.execvpe", forbidden)
    monkeypatch.setattr("socket.create_connection", forbidden)
    monkeypatch.setattr("pbatch.llm_pool_secrets.read_secret", forbidden)
    monkeypatch.setattr("pbatch.llm_pool_store.PoolStore", forbidden)
    before = dict(os.environ)
    contents = path.read_bytes()
    files = sorted(str(item) for item in path.parent.rglob("*"))
    assert pool_main(["--config", str(path), "run", "--model", "glm-5.1", "--dry-run",
                      "--", secret, "--api-key", secret]) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["action"] == "run" and report["dry_run"] is True
    assert report["environment_changes"] == [INLINE_ENV]
    assert not report["command_started"] and not report["credentials_read"]
    assert not report["runtime_state_changed"] and report["argument_count"] == 3
    assert secret not in output.out + output.err and "private-provider-name" not in output.out
    assert str(path.parent) not in output.out and not output.err
    assert dict(os.environ) == before and path.read_bytes() == contents
    assert sorted(str(item) for item in path.parent.rglob("*")) == files


def test_launcher_execs_without_shell_or_mutating_parent_environment(pool_file, monkeypatch, capsys):
    path, _ = pool_file
    before = dict(os.environ)
    command = ["--", "fake-opencode", "run", "$(do-not-execute); & literal *", ""]
    class Executed(BaseException):
        pass
    def fake_exec(program, arguments, environment):
        assert program == "fake-opencode" and arguments == command[1:]
        assert json.loads(environment[INLINE_ENV])["model"] == "opencode-go/glm-5.1"
        assert dict(os.environ) == before
        raise Executed
    monkeypatch.setattr("os.execvpe", fake_exec)
    with pytest.raises(Executed):
        pool_main(["--config", str(path), "run", "--model", "glm-5.1", *command])
    output = capsys.readouterr()
    assert output.out == output.err == ""
    assert dict(os.environ) == before


@pytest.mark.parametrize("error,code", [(FileNotFoundError("raw-secret-filename"), 127),
                                      (PermissionError("raw-secret-filename"), 126),
                                      (OSError(errno.E2BIG, "raw-secret-environment"), 126),
                                      (ValueError("raw-secret-argument"), 126)])
def test_exec_failures_are_sanitized_without_printing_arguments(pool_file, monkeypatch, capsys, error, code):
    path, _ = pool_file
    def failing_exec(*args):
        raise error
    monkeypatch.setattr("os.execvpe", failing_exec)
    assert pool_main(["--config", str(path), "run", "--model", "glm-5.1",
                      "--", "raw-secret-command", "raw-secret-argument"]) == code
    output = capsys.readouterr()
    assert not output.out and "raw-secret" not in output.err


def test_invalid_inherited_config_stops_before_exec(pool_file, monkeypatch, capsys):
    path, _ = pool_file
    monkeypatch.setenv(INLINE_ENV, '{"sensitive-field-secret": [broken-json')
    monkeypatch.setattr("os.execvpe", lambda *a: pytest.fail("invalid merge must not launch"))
    assert pool_main(["--config", str(path), "run", "--model", "glm-5.1", "--", "fake"]) == 2
    assert "sensitive-field-secret" not in capsys.readouterr().err
