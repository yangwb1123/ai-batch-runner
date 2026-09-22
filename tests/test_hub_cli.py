import json
import sys
import types

from pbatch import hub_cli
from pbatch.cli import _SUBCOMMANDS


class _Client:
    def list_sessions(self, instance_id=""):
        return [{"instance_id": instance_id, "session_id": "ses-1"}]


def test_fleet_registered_and_sessions_action_uses_client(monkeypatch, capsys):
    assert _SUBCOMMANDS["fleet"] == "hub_cli:hub_main"
    monkeypatch.setattr(hub_cli.HubClient, "from_env",
                        lambda *_args, **_kwargs: _Client())
    result = hub_cli.hub_main(["sessions", "--hub", "https://hub.example",
                               "--instance-id", "inst-1", "--json"])
    assert result == 0
    assert json.loads(capsys.readouterr().out)[0]["session_id"] == "ses-1"


def test_init_prints_paths_without_secret_material(tmp_path, monkeypatch, capsys):
    setup = types.ModuleType("pbatch.hub_setup")
    setup.create_development_config = lambda directory, instances: {
        "config": str(directory) + "/hub.json", "user_token_file": "/private/user.token",
        "gateway_token_files": {name: f"/private/{name}.token" for name in instances},
        "user_token": "must-not-print", "gateway_tokens": {"alpha": "hidden"},
    }
    monkeypatch.setitem(sys.modules, "pbatch.hub_setup", setup)
    result = hub_cli.hub_main(["init", "--directory", str(tmp_path),
                               "--instances", "alpha", "beta"])
    output = capsys.readouterr().out
    record = json.loads(output)
    assert result == 0
    assert "must-not-print" not in output and "hidden" not in output
    assert record["gateway_token_files"] == {
        "alpha": "/private/alpha.token", "beta": "/private/beta.token"}


def test_serve_delegates_to_http_service_only_on_request(monkeypatch):
    service = types.ModuleType("pbatch.hub_http")
    service.hub_serve_main = lambda argv: 37 if argv == ["--port", "8788"] else 1
    monkeypatch.setitem(sys.modules, "pbatch.hub_http", service)
    assert hub_cli.hub_main(["serve", "--port", "8788"]) == 37


def test_actions_have_explicit_help():
    actions = hub_cli._parser()._subparsers._group_actions[0].choices
    assert set(("instances", "sessions", "show", "send", "events", "cancel",
                "agent", "tui", "init")).issubset(actions)


def test_gateway_forwards_oauth_profile_without_printing_credentials(monkeypatch, capsys):
    calls = []
    client = object()

    def from_env(url, **kwargs):
        calls.append((url, kwargs))
        return client

    class Gateway:
        instance_id = "alpha"
        generation = 1

        def __init__(self, options, **kwargs):
            assert kwargs["client"] is client
            assert options.instance_id == "alpha"
            assert options.sessions == 0 and options.max_sessions == 3

        def run_forever(self):
            calls.append("run")

        def close(self):
            calls.append("closed")

    monkeypatch.setattr(hub_cli.HubClient, "from_env", from_env)
    monkeypatch.setattr(hub_cli, "HubGateway", Gateway)
    result = hub_cli.hub_main([
        "agent", "--hub", "https://hub.example", "--instance-id", "alpha",
        "--oauth-config", "/private/gateway-oauth.json", "--sessions", "0", "--max-sessions", "3"])
    assert result == 0
    assert calls[0] == ("https://hub.example", {
        "timeout": 5.0, "oauth_config": "/private/gateway-oauth.json"})
    assert calls[1:] == ["run", "closed"]
    output = capsys.readouterr()
    assert "Gateway alpha registered" in output.out
    assert "/private" not in output.out + output.err
