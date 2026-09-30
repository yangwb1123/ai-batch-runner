"""App Server must execute the selected CLI, including paths outside PATH."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import pytest

from pbatch import config
from pbatch.tui import PiBatchTUI, TuiOptions
from pbatch.tui_appserver_support import app_server_argv


PROVIDER = """\
import json
from pathlib import Path
import sys

marker = Path(__file__).with_suffix('.json')
methods = []
for line in sys.stdin:
    request = json.loads(line)
    methods.append(request['method'])
    marker.write_text(json.dumps({'argv': sys.argv, 'methods': methods,
                                  'cwd': str(Path.cwd())}))
    if 'id' not in request:
        continue
    result = {'thread': {'id': 'fake-thread'}} if request['method'] == 'thread/start' else {}
    print(json.dumps({'id': request['id'], 'result': result}), flush=True)
"""


def _provider(directory):
    directory.mkdir()
    executable = directory / "codex"
    executable.write_text(f"#!{sys.executable}\n{PROVIDER}", encoding="utf-8")
    executable.chmod(0o755)
    return executable


@pytest.mark.skipif(os.name != "posix", reason="executable fixture uses a POSIX shebang")
@pytest.mark.parametrize("selection", ["explicit", "configured", "default"])
def test_tui_spawns_selected_appserver_executable(tmp_path, monkeypatch, selection):
    selected = _provider(tmp_path / "selected provider")
    path_provider = _provider(tmp_path / "path provider")
    monkeypatch.setenv("PATH", str(path_provider.parent))
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    monkeypatch.setattr(config, "AGENT_BIN", str(selected) if selection == "configured" else "pi")
    options = TuiOptions(cwd=str(tmp_path), runtime="app-server", memory_mode="off",
                         agent=str(selected) if selection == "explicit" else "")
    expected = path_provider if selection == "default" else selected

    async def scenario():
        app = PiBatchTUI(options)
        async with app.run_test(size=(100, 30)):
            assert not app._runtime_blocked_reason
            assert app._adapter is not None
            transport = app._adapter._transport
            assert transport._process.args == [
                "codex" if selection == "default" else str(selected), "app-server"]
            evidence = json.loads(expected.with_suffix(".json").read_text())
            assert Path(evidence["argv"][0]).resolve() == expected
            assert evidence["argv"][1:] == ["app-server"]
            assert evidence["cwd"] == str(tmp_path)
            assert evidence["methods"] == ["initialize", "initialized", "thread/start"]
            unused = selected if selection == "default" else path_provider
            assert not unused.with_suffix(".json").exists()
        assert transport._process.poll() is not None
        assert not transport._stderr_thread.is_alive()

    asyncio.run(scenario())


@pytest.mark.skipif(os.name != "posix", reason="executable fixture uses a POSIX shebang")
def test_missing_explicit_appserver_executable_does_not_fall_back_to_path(tmp_path, monkeypatch):
    path_provider = _provider(tmp_path / "path provider")
    monkeypatch.setenv("PATH", str(path_provider.parent))
    monkeypatch.setenv("PBATCH_DATA_DIR", str(tmp_path / "runtime"))
    missing = tmp_path / "not installed" / "codex"

    async def scenario():
        app = PiBatchTUI(TuiOptions(cwd=str(tmp_path), runtime="app-server",
                                    memory_mode="off", agent=str(missing)))
        async with app.run_test(size=(100, 30)):
            assert app._adapter is None
            assert str(missing) in app._runtime_blocked_reason
            assert not path_provider.with_suffix(".json").exists()

    asyncio.run(scenario())


def test_explicit_appserver_proxy_uses_selected_executable():
    assert app_server_argv("/tmp/codex.sock", agent="/opt/private bin/codex") == [
        "/opt/private bin/codex", "app-server", "proxy", "--sock", "/tmp/codex.sock"]
