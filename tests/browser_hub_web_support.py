"""Real Chrome + TLS/HTTP/SDK/SQLite/Gateway/AgentSession, synthetic identity/provider only."""

import os
import re
import shutil
import ssl
import threading
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from pbatch.agent_session import AgentSession
from pbatch.hub_auth import GATEWAY_SCOPE
from pbatch.hub_client import HubClient, _NoRedirect
from pbatch.hub_gateway import HubGateway, HubGatewayConfig
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_store import HubStore
from pbatch.hub_web import HubWeb
from browser_hub_web import local_tls, issuer_page
from test_hub_web_control import SCOPES
from test_hub_integration import _Runtime


def until(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate(), "synthetic local runtime did not converge within five seconds"


class StartedRuntime(_Runtime):
    """Observe fixture-provider start separately from the earlier Hub acknowledgement."""
    def __init__(self):
        super().__init__(hold=True)
        self.started = threading.Event()

    def start_turn(self, request):
        handle = super().start_turn(request)
        self.started.set()
        return handle

    def emit(self, kind, payload=None):
        super().emit(kind, payload)
        if kind == "turn.failed":
            self.handle._done.set()


def task_gateway(origin, auth, signed, tmp_path, *, publish_command_output=False, share_files=(),
                 gateway_run_forever=False):
    auth.grants.append(replace(auth.grants[0], subject="gateway-alpha", client_id="gateway-alpha",
        kind="gateway", scopes=frozenset({GATEWAY_SCOPE})))
    token = signed[1](sub="gateway-alpha", client_id="gateway-alpha", scope=GATEWAY_SCOPE, exp=int(time.time()) + 120)
    client = HubClient(origin, token)
    # Only the ephemeral fixture uses a self-signed cert; product TLS policy is unchanged.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    client._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect(),
                                                urllib.request.HTTPSHandler(context=context))
    project = tmp_path / "project"
    project.mkdir()
    runtimes = []

    def session(options):
        runtime = StartedRuntime()
        runtimes.append(runtime)
        return AgentSession(runtime, options)

    options = HubGatewayConfig(origin, cwd=str(project), instance_id="alpha", project_id="synthetic-project",
        state_dir=str(tmp_path / "gateway-state"), sessions=0, max_sessions=2,
        publish_command_output=publish_command_output, share_files=share_files)
    if not gateway_run_forever:
        options = replace(options, poll_interval=0.1)
    gateway = HubGateway(options, client=client, session_factory=session)
    gateway.start()
    return gateway, runtimes


def gateway_worker(gateway, stop, errors, run_forever):
    """The drain fixture uses the real loop; existing fixtures retain their 20 ms ticks."""
    def tick():
        try:
            if run_forever:
                gateway.run_forever()
            else:
                while not stop.wait(0.02):
                    gateway.run_once()
        except BaseException as error:
            requested_stop = (run_forever and stop.is_set() and gateway._stop.is_set()
                              and isinstance(error, RuntimeError) and str(error) == "Hub Gateway is stopped")
            if not requested_stop:
                errors.append(type(error).__name__)
            stop.set()

    worker = threading.Thread(target=tick, daemon=True)
    worker.start()
    return worker


def task_fixture_access(settings, auth, transport, signed, options):
    """Only explicitly selected browser fixtures receive file-content authority."""
    settings["web"]["control"] = True
    scopes = set(SCOPES)
    files = options.get("share_files", ()) if isinstance(options, dict) else ()
    if files:
        settings["web"]["file_snapshots"] = True
        scopes.add("agent.artifacts:read")
    auth.grants = [replace(grant, scopes=frozenset(scopes)) for grant in auth.grants]
    transport.token = lambda: signed[1](scope=" ".join(sorted(scopes)), exp=int(time.time()) + 120)
    return files


@pytest.fixture
def controlled_hub(tmp_path, web_env, signed, request):
    old, auth, transport, settings, _ = web_env
    old.close()
    options = getattr(request, "param", False)
    run_forever = options.get("gateway_run_forever", False) if isinstance(options, dict) else False
    files = task_fixture_access(settings, auth, transport, signed, options)
    store = HubStore(tmp_path / "hub.sqlite3")
    server = HubHTTPServer(("127.0.0.1", 0), store, auth, tls_context=local_tls(tmp_path))
    origin = f"https://127.0.0.1:{server.server_port}"
    settings["web"]["public_origin"], settings["allowed_origins"] = origin, [origin]
    server.web = HubWeb(settings, auth, transport=transport)
    server.allowed_origins = frozenset([origin])
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    stop, errors = threading.Event(), []
    gateway = worker = None
    try:
        publish_output = options.get("publish_command_output", False) if isinstance(options, dict) else options
        gateway, runtimes = task_gateway(origin, auth, signed, tmp_path,
            publish_command_output=publish_output, share_files=files, gateway_run_forever=run_forever)
        worker = gateway_worker(gateway, stop, errors, run_forever)
        yield origin, store, runtimes, tmp_path
        assert not errors
    finally:
        stop.set()
        if run_forever and gateway is not None:
            # Stop only this fixture's loop; keep its store open until late HTTP returns.
            gateway._stop.set()
        if worker is not None:
            worker.join(timeout=5)
            assert not worker.is_alive()
        if gateway is not None:
            gateway.close()
        server.shutdown()
        server.server_close()
        server.web.close()
        server_thread.join(timeout=5)
        store.close()
        assert not errors


@pytest.fixture
def task_page(controlled_hub, request):
    """Real authenticated browser; the provider and issuer remain explicit fixtures."""
    origin, store, runtimes, tmp_path = controlled_hub
    width, height = getattr(request, "param", (1440, 1000))
    executable = shutil.which("google-chrome") or shutil.which("chromium")
    assert executable, "install Chrome/Chromium before running this explicit smoke"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=executable, headless=True, chromium_sandbox=True)
        context = browser.new_context(ignore_https_errors=True, viewport={"width": width, "height": height})
        context.route("https://identity.test/**", issuer_page)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.on("console", lambda message: errors.append("CSP rejection")
                if message.type == "error" and "Content Security Policy" in message.text else None)
        page._abr_dialog_handler = lambda dialog: dialog.accept() if dialog.type == "beforeunload" else (
            errors.append("Unexpected browser dialog"), dialog.dismiss())
        page.on("dialog", page._abr_dialog_handler)
        try:
            page.goto(origin)
            page.get_by_role("button", name="使用 Snaplink 登录").click()
            page.get_by_role("button", name="Synthetic issuer approval").click()
            page.get_by_role("button", name="Agent 任务", exact=True).click()
            expect(page.get_by_role("button", name="新建任务", exact=True)).to_be_enabled()
            yield page, context, store, runtimes
            assert not errors, errors
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        finally:
            target = Path(os.environ.get("PBATCH_WEB_BROWSER_ARTIFACTS", str(tmp_path)))
            target.mkdir(parents=True, exist_ok=True)
            name = re.sub(r"[^a-zA-Z0-9_-]", "_", request.node.name)
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(target / f"{name}-{width}.png"), full_page=True)
            context.close()
            browser.close()


def create_task(page, store, name="browser-task"):
    page.get_by_role("button", name="新建任务", exact=True).click()
    page.get_by_label("目标实例 / 固定项目").select_option("alpha")
    page.get_by_label("任务名称", exact=True).fill(name)
    page.get_by_role("button", name="创建任务", exact=True).click()
    until(lambda: any(row["name"] == name for row in store.sessions("team", "alpha")))
    session = next(row["session_id"] for row in store.sessions("team", "alpha") if row["name"] == name)
    expect(page).to_have_url(re.compile(r"#tasks/" + re.escape(session) + r"$"))
    expect(page.get_by_label("发送下一条指令")).to_be_enabled()
    return session


def send_prompt(page, store, runtimes, session, prompt, *, keyboard=False):
    page.get_by_label("发送下一条指令").fill(prompt)
    button = page.get_by_role("button", name="发送指令", exact=True)
    if keyboard:
        button.focus()
        page.keyboard.press("Enter")
    else:
        button.click()
    until(lambda: bool(runtimes) and prompt in runtimes[-1].prompts
          and bool(store.session("team", session)["active_turn_id"]))
    return store.session("team", session)["active_turn_id"]


def complete_turn(runtime, text):
    runtime.emit("message.completed", {"text": text})
    runtime.emit("turn.completed")
