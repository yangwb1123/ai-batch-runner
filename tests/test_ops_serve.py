"""ops serve HTTP monitor tests: endpoints, auto-refresh page, token."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from pbatch import config as config_mod
from pbatch.ops_serve import OpsHandler


@pytest.fixture()
def server(tmp_path):
    data_dir = config_mod.data_dir(str(tmp_path))
    (data_dir / "agents").mkdir(parents=True, exist_ok=True)
    (data_dir / "agents" / "ledger.jsonl").write_text(
        '{"ts":"2026-08-22T09:00:00Z","agent":"pi","status":"succeeded",'
        '"attempt":1,"task_brief":"hello","outputs":["a.md"],'
        '"elapsed_ms":10,"exit_code":0}\n'
        '{"ts":"2026-08-22T09:01:00Z","agent":"codex","status":"rejected",'
        '"attempt":2,"task_brief":"retry me","outputs":[],'
        '"elapsed_ms":90,"exit_code":0}\n',
        encoding="utf-8")
    OpsHandler.data_dir = str(data_dir)
    OpsHandler.cwd = str(tmp_path)
    OpsHandler.token = "sekret"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), OpsHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    httpd.server_close()


def _get(base: str, path: str, token: str = "sekret") -> dict:
    request = urllib.request.Request(base + path)
    request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def test_overview_and_runs_endpoints(server):
    overview = _get(server, "/api/overview")
    assert overview["total"] == 2
    assert overview["status_counts"]["rejected"] == 1
    runs = _get(server, "/api/runs?limit=5")["runs"]
    assert len(runs) == 2
    assert runs[0]["agent"] == "pi" and runs[0]["attempt"] == 1
    codex = _get(server, "/api/runs?agent=codex")["runs"]
    assert len(codex) == 1 and codex[0]["status"] == "rejected"


def test_log_and_messages_endpoints(server, tmp_path):
    log_file = tmp_path / "run.log"
    log_file.write_text("line-one\nGATE FAIL\nline-three\n", encoding="utf-8")
    with open(log_file, "a", encoding="utf-8"):
        pass
    data = _get(server, f"/api/log?file={log_file}&grep=GATE")
    assert data["lines"] == ["GATE FAIL"]
    outside = tmp_path.parent / "outside.log"
    outside.write_text("secret\n", encoding="utf-8")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server, f"/api/log?file={outside}")
    assert exc.value.code == 400
    messages = _get(server, "/api/messages?limit=3")
    assert isinstance(messages["messages"], list)


def test_auto_refresh_page(server):
    request = urllib.request.Request(server + "/")
    request.add_header("Authorization", "Bearer sekret")
    with urllib.request.urlopen(request, timeout=10) as response:
        page = response.read().decode("utf-8")
    assert "运营控制台" in page
    assert "setInterval(()=>location.reload(),30000)" in page  # auto refresh
    assert "rejected" in page


def test_token_required_and_unknown_route(server):
    request = urllib.request.Request(server + "/api/overview")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=10)
    assert exc.value.code == 401
    request2 = urllib.request.Request(server + "/api/nope")
    request2.add_header("Authorization", "Bearer sekret")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request2, timeout=10)
    assert exc.value.code == 404