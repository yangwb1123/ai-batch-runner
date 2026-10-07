"""Global HTTP task view: explicit scope, authentication, no control or raw bodies."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from pbatch import ops, ops_serve, task_sources


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("PBATCH_RUNTIME_HOME", str(tmp_path / "home"))
    data = tmp_path / "custom"; (data / "agents").mkdir(parents=True)
    (data / "agents" / "ledger.jsonl").write_text(json.dumps({
        "run_id": "run", "task_id": "task", "status": "succeeded", "attempt": 1,
        "phase": "execution", "task_brief": "<script>alert(1)</script>",
        "ts": "2026-10-10T00:00:00Z", "usage": {"totalTokens": 99},
    }) + "\n", encoding="utf-8")
    task_sources.register_source(task_sources.source("custom", str(data)))

    class Handler(ops_serve.OpsHandler):
        global_tasks = True
        token = "private-token"
        cwd = str(tmp_path)
        data_dir = str(data)
        control = None

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", Handler
    httpd.shutdown(); httpd.server_close(); thread.join(timeout=5)


def request(server, route, token="private-token", method="GET"):
    base, _ = server
    value = urllib.request.Request(base + route, method=method,
                                   data=b"{}" if method == "POST" else None)
    if token:
        value.add_header("Authorization", "Bearer " + token)
    return urllib.request.urlopen(value, timeout=5)


def test_global_json_and_detail_share_identity_and_coverage(server):
    with request(server, "/api/tasks") as response:
        assert response.headers["Cache-Control"] == "no-store"
        view = json.loads(response.read())
    assert view["schema_version"] == 1
    task = view["tasks"][0]
    assert task["usage"]["total_tokens"] == 99
    with request(server, "/api/tasks/" + task["task_id"]) as response:
        detail = json.loads(response.read())
    assert detail["task_id"] == task["task_id"]
    assert detail["capabilities"]["control"] is False


def test_global_page_never_embeds_project_content_as_script(server):
    with request(server, "/") as response:
        page = response.read().decode()
        assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
    assert "全局任务中心" in page
    assert "setInterval(refresh,5000)" in page
    assert "alert(1)" not in page
    assert "td.textContent=text" in page


@pytest.mark.parametrize("token", ["", "wrong"])
def test_global_routes_require_authentication(server, token):
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/tasks", token=token)
    assert exc.value.code == 401


def test_default_workspace_server_does_not_gain_global_scope(server):
    _, handler = server
    handler.global_tasks = False
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/tasks")
    assert exc.value.code == 404


def test_http_cannot_supply_a_filesystem_path_or_execute_cleanup(server):
    with request(server, "/api/tasks?source=/etc&file=/etc/passwd") as response:
        view = json.loads(response.read())
    assert view["tasks"] == [] and view["diagnostics"] == ["task source not found"]
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/tasks/tc-test/cleanup", method="POST")
    assert exc.value.code == 403


@pytest.mark.parametrize("route", ["/api/tasks?limit=0", "/api/tasks?limit=-1",
                                    "/api/tasks?limit=nan", "/api/tasks/..%2Fetc%2Fpasswd"])
def test_invalid_request_limits_and_paths_fail_closed(server, route):
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, route)
    assert exc.value.code == 400


def test_global_server_requires_token_and_cannot_enable_control(tmp_path, monkeypatch):
    monkeypatch.delenv("PBATCH_OPS_TOKEN", raising=False)
    monkeypatch.delenv("PBATCH_OPS_TOKEN_FILE", raising=False)
    with pytest.raises(SystemExit) as exc:
        ops_serve.serve_main(["--global"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        ops_serve.serve_main(["--global", "--control", "--token", "secret"])
    assert exc.value.code == 2


def test_ops_cli_forwards_global_scope_without_changing_existing_defaults(monkeypatch):
    calls = []
    monkeypatch.setattr(ops_serve, "serve_main", lambda values: calls.append(values) or 0)
    assert ops.ops_main(["serve", "--global", "--token", "secret"]) == 0
    assert "--global" in calls[-1]
    assert ops.ops_main(["serve"]) == 0
    assert "--global" not in calls[-1]


def test_repair_preview_is_identical_in_cli_snapshot_and_authenticated_detail(server, capsys):
    from pathlib import Path
    from pbatch import task_center
    from pbatch.task_state_io import atomic_write

    _, handler = server
    root = Path(handler.data_dir)
    source = task_sources.source("custom", str(root))
    checkpoint = {"schema_version": 1, "kind": "pipeline", "run_id": "pl-" + "b" * 32,
        "source_id": source["source_id"], "name": "failure", "status": "failed",
        "started_at": 100, "ts": "2026-10-10T00:00:10Z", "seq": 3,
        "plan_complete": True, "stage": "gate", "parent_run_id": "", "parent_source_id": "",
        "process_run_id": "process", "steps": [
            {"id": "gate", "state": "failed", "gate": True, "dependencies": []},
            {"id": "release", "state": "pending", "gate": True, "dependencies": ["gate"]}]}
    (root / "tasks").mkdir()
    (root / "tasks/lifecycle.jsonl").write_text(json.dumps(checkpoint) + "\n")
    (root / "tasks/runs").mkdir()
    atomic_write(root / "tasks/runs" / (checkpoint["run_id"] + ".json"), checkpoint)
    direct = next(task for task in task_center.snapshot()["tasks"] if task["kind"] == "pipeline")
    with request(server, "/api/tasks") as response:
        view = json.loads(response.read())
    http_task = next(task for task in view["tasks"] if task["task_id"] == direct["task_id"])
    with request(server, "/api/tasks/" + direct["task_id"]) as response:
        detail = json.loads(response.read())
    assert ops.ops_main(["tasks", "--id", direct["task_id"]]) == 0
    cli = json.loads(capsys.readouterr().out)
    assert cli["repair_preview"] == detail["repair_preview"] == http_task["repair_preview"] == direct["repair_preview"]
    assert detail["repair_preview"]["candidate_stage"]["declared_gate"] is True
    assert detail["repair_preview"]["authority"]["grants_execution"] is False
    assert detail["repair_preview"]["remaining_declared_gates"] == 2


@pytest.mark.parametrize("action", ["repair", "resume", "retry", "clear-budget", "provider-breaker-reset"])
def test_repair_preview_never_enables_http_actions(server, action):
    with request(server, "/api/tasks") as response:
        task = json.loads(response.read())["tasks"][0]
    with pytest.raises(urllib.error.HTTPError) as exc:
        request(server, "/api/tasks/" + task["task_id"] + "/" + action, method="POST")
    assert exc.value.code == 403
    assert task["repair_preview"]["authority"]["grants_execution"] is False
