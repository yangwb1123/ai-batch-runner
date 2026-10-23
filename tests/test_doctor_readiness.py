"""Doctor readiness is informational and never exposes probe credentials."""

import json
import sys

import pytest

from pbatch import agent_readiness, config, lock, product_cli
from pbatch.agent_ledger import DEFAULT_AGENTS


@pytest.fixture(autouse=True)
def _doctor_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("PBATCH_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "runtime"))
    monkeypatch.setattr(config, "AGENT_BIN", sys.executable)
    monkeypatch.setattr(product_cli, "_doctor_config_checks",
                        lambda: [("项目配置", True, "可用")])
    monkeypatch.setattr(lock, "_lock_path", lambda cwd: tmp_path / "instance.lock")


def _probe_result(agent, status):
    reason = {
        "ready": "ready-reason-must-not-be-printed",
        "not_ready": "login required token=doctor-token password: doctor-password",
        "unknown": "probe unavailable Bearer doctor-bearer",
    }[status]
    return agent_readiness.ReadinessResult(
        agent=agent, status=status, source="agent.readiness_cmd",
        argv=("probe", "--api-key", "doctor-api-key"), reason=reason,
        exit_code=None, timed_out=False, duration_ms=None, tail="doctor-private-tail")


@pytest.mark.parametrize("binary_ok", [True, False])
def test_doctor_readiness_statuses_are_informational(monkeypatch, capsys, binary_ok):
    monkeypatch.setattr(product_cli.shutil, "which",
                        lambda binary: sys.executable if binary_ok else None)
    checks = product_cli._doctor_checks()
    calls = []

    def fake_probe(agent):
        status = agent_readiness.STATUSES[len(calls) % 3]
        calls.append(agent)
        return _probe_result(agent, status)

    monkeypatch.setattr(agent_readiness, "probe", fake_probe)
    with pytest.raises(SystemExit) as result:
        product_cli.doctor_main([])
    assert result.value.code == (0 if binary_ok else 1)
    assert calls == list(DEFAULT_AGENTS)
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.splitlines()
    readiness = [line for line in lines if "[INFO] agent readiness " in line]
    assert len(readiness) == len(DEFAULT_AGENTS)
    for index, (agent, line) in enumerate(zip(DEFAULT_AGENTS, readiness)):
        assert f"{agent}: {agent_readiness.STATUSES[index % 3]}" in line
    assert "login required token=<redacted> password: <redacted>" in captured.out
    assert "probe unavailable Bearer <redacted>" in captured.out
    for secret in ("doctor-token", "doctor-password", "doctor-bearer",
                   "doctor-api-key", "doctor-private-tail", "ready-reason-must-not-be-printed"):
        assert secret not in captured.out
    legacy = [f"  [{'OK' if ok else 'WARN'}] {name:<18} {detail}"
              for name, ok, detail in checks]
    insertion = next(index + 1 for index, (name, _, _) in enumerate(checks)
                     if name == f"agent 二进制 {config.AGENT_BIN}")
    assert lines == legacy[:insertion] + readiness + legacy[insertion:]


def test_doctor_json_output_is_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(agent_readiness, "probe",
                        lambda *args, **kwargs: pytest.fail("JSON must not run probes"))
    checks = product_cli._doctor_checks()
    with pytest.raises(SystemExit) as result:
        product_cli.doctor_main(["--json"])
    assert result.value.code == 0
    assert json.loads(capsys.readouterr().out) == [
        {"name": name, "ok": ok, "detail": detail} for name, ok, detail in checks]
