"""Operations-console command contracts."""

from __future__ import annotations

import json


def test_breaker_reset_is_explicit_and_reports_state(monkeypatch, tmp_path, capsys):
    from pbatch import config, ops, provider_breaker

    state = {
        "active": True, "cooldown_remaining_s": 30.0,
        "streak": 0, "step": 1, "threshold": 5,
        "shared_state_file": str(tmp_path / "provider-breaker.json"),
    }
    calls = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "data_dir", lambda _cwd="": tmp_path)

    def reset(data_dir, provider=""):
        calls.append((data_dir, provider))
        state.update(active=False, cooldown_remaining_s=0.0, step=0)

    monkeypatch.setattr(provider_breaker, "register_success", reset)
    monkeypatch.setattr(
        provider_breaker, "breaker_state", lambda data_dir, provider="": dict(state))

    assert ops.ops_main(["breaker", "--reset", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert calls == [(str(tmp_path), "")]
    assert payload["active"] is False
    assert payload["cooldown_remaining_s"] == 0.0
