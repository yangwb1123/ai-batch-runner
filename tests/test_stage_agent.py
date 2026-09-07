"""Stage-level agent selection: each pipeline stage may run a different
executor (T13). Per-task override, never a global swap — parallel-safe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pbatch import pipeline as pipeline_mod
from pbatch.models import Stage, Task


def _agent(tmp_path: Path, marker: str, name: str) -> Path:
    path = tmp_path / name
    path.write_text(
        "#!/bin/sh\n"
        f"echo '{marker}' $0\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_stage_agent_flows_into_task_and_argv(tmp_path):
    agent_a = _agent(tmp_path, "agent-a", "a.sh")
    agent_b = _agent(tmp_path, "agent-b", "b.sh")
    stage = Stage(name="s1", agent=str(agent_b), from_prompt="hi",
                  output=str(tmp_path / "out.md"))
    tasks, _reused, _early = pipeline_mod._build_stage_tasks(
        stage, {}, False, "", 0, "")
    assert len(tasks) == 1
    assert tasks[0].agent == str(agent_b)
    cmd = tasks[0].to_cmd()
    assert cmd[0] == str(agent_b)
    # the global default is untouched (parallel-safe)
    assert Task("x").agent == ""


def test_task_def_agent_override_wins_over_stage(tmp_path):
    upstream = tmp_path / "up.md"
    upstream.write_text("input\n", encoding="utf-8")
    stage = Stage(name="s1", agent="/bin/false", from_outputs="prev",
                  tasks=[{"prompt": "p", "output": "o.md",
                          "agent": "/bin/true"}])
    tasks, _reused, _early = pipeline_mod._build_stage_tasks(
        stage, {"prev": [str(upstream)]}, False, "", 0, "")
    assert tasks[0].agent == "/bin/true"


def test_meta_orchestrator_and_roles_inherit_stage_agent(tmp_path):
    from pbatch import meta

    stage = Stage(name="review", agent="codex-stage", meta=True,
                  output_dir=str(tmp_path), cwd=str(tmp_path))
    orchestrator = meta._build_meta_prompt_task(
        stage, [], "evidence", "", 0)
    role_tasks, ok = meta._build_role_tasks(
        stage, [{"role": "reviewer", "task": "inspect evidence"}],
        "evidence", "", 0)

    assert ok is True
    assert orchestrator.agent == "codex-stage"
    assert role_tasks[0][1].agent == "codex-stage"


def test_runner_uses_task_agent_for_limits_failures_and_diagnostics(
        monkeypatch):
    from types import SimpleNamespace
    from pbatch import config, runner
    from pbatch.runner_results import prompt_limit_result

    monkeypatch.setattr(config, "AGENT_BIN", "pi-global")
    monkeypatch.setattr(config, "_AGENT_CFG", {
        "argument_flags": {"prompt": "-p"},
        "error_patterns": ["global-only"],
    })
    monkeypatch.setattr(config, "_AGENTS_CFG", {"codex-stage": {
        "argument_flags": {"prompt": ""},
        "extra_args": ["--long-transport-option"],
        "error_patterns": ["stage-only"],
    }})
    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 30)
    task = Task(prompt="ok", agent="codex-stage")

    assert prompt_limit_result(task, task.to_cmd()) is None
    failed = runner._result_from_proc(
        task, SimpleNamespace(returncode=0), ["ordinary output\n"],
        ["stage-only\n"], 0.1)
    assert failed.success is False
    assert "stage-only" in failed.reason
    missing = runner._agent_not_found(task)
    assert "codex-stage not found" in missing.stderr


def _codex_events(message="answer", terminal="turn.completed"):
    thread_id = "01a0272d-66ab-74f1-8891-1013ac2cb87a"
    return [
        json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n",
        json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": message}}) + "\n",
        json.dumps({"type": terminal}) + "\n",
    ]


def _enable_jsonl_adapter(monkeypatch, config):
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    monkeypatch.setattr(config, "_AGENTS_CFG", {"codex-json": {
        "argument_flags": {"prompt": ["exec"]},
        "capture_thread_id": True,
        "output_protocol": "codex-jsonl",
        "session_flags": {"start": ["exec", "{prompt}"]},
        "extra_args": ["--json"],
        "error_patterns": ["connection refused", "codex: "],
        "provider_error_patterns": ["connection refused", "codex: "],
    }})


@pytest.mark.parametrize(("lines", "reason"), [
    (["not-json\n"], "malformed"),
    (["[]\n"], "not an object"),
    (_codex_events()[:1] + [json.dumps({"type": "turn.started"}) + "\n"],
     "no turn.completed"),
    (_codex_events(terminal="turn.failed"), "ended with turn.failed"),
    (_codex_events() + [json.dumps({"type": "turn.started"}) + "\n"],
     "events after terminal"),
    (_codex_events()[:1] + [json.dumps({"type": "turn.completed"}) + "\n"],
     "no final agent message"),
    (_codex_events()[:1] + [json.dumps({"type": "item.completed", "item": {
        "type": "agent_message", "text": None}}) + "\n"], "is not text"),
])
def test_codex_jsonl_transport_fails_closed(lines, reason):
    from pbatch.runner_results import codex_transcript_output

    _body, error = codex_transcript_output(lines)

    assert reason in error


def test_codex_jsonl_keeps_only_final_completed_message():
    from pbatch.runner_results import codex_transcript_output

    lines = _codex_events("first")[:-1]
    lines.extend(_codex_events("final")[1:])

    assert codex_transcript_output(lines) == ("final", "")


def test_successful_codex_body_is_not_transport_or_provider_failure(
        monkeypatch):
    """A completed transcript makes even a short outage discussion prose.

    Plain-text adapters retain the legacy short-reply rejection protocol;
    structured Codex bodies are protected by the explicit turn terminal.
    """
    from types import SimpleNamespace
    from pbatch import config, runner

    _enable_jsonl_adapter(monkeypatch, config)
    failures, successes = [], []
    monkeypatch.setattr(runner, "_pb_register_fail",
                        lambda reason, provider="": failures.append(reason))
    monkeypatch.setattr(runner, "_pb_register_ok",
                        lambda provider="": successes.append(provider))
    lines = _codex_events("connection refused; codex: is discussed")
    lines.insert(1, json.dumps({"type": "item.completed", "item": {
        "type": "command_execution", "aggregated_output":
        "connection refused from codex: transport"}}) + "\n")

    result = runner._result_from_proc(
        Task("review", agent="codex-json"),
        SimpleNamespace(returncode=0), lines, [], 0.1)

    assert result.success is True
    assert result.stdout == "connection refused; codex: is discussed"
    assert failures == []
    assert len(successes) == 1


def test_unrelated_failed_codex_turn_ignores_prior_provider_prose(monkeypatch):
    from types import SimpleNamespace
    from pbatch import config, runner

    _enable_jsonl_adapter(monkeypatch, config)
    failures = []
    monkeypatch.setattr(runner, "_pb_register_fail",
                        lambda reason, provider="": failures.append(reason))
    lines = _codex_events("assistant discusses connection refused")[:-1]
    lines.insert(1, json.dumps({
        "type": "item.completed", "item": {
            "type": "command_execution",
            "aggregated_output": "connection refused in a local command",
        }}) + "\n")
    lines.append(json.dumps({
        "type": "turn.failed", "error": "unrelated validation failure",
    }) + "\n")

    result = runner._result_from_proc(
        Task("review", agent="codex-json"),
        SimpleNamespace(returncode=0), lines, [], 0.1)

    assert result.success is False
    assert "turn.failed" in result.reason
    assert result.stdout == "".join(lines)
    assert failures == []


@pytest.mark.parametrize("terminal", [
    {"type": "turn.failed", "error": {
        "message": "details " * 2000 + "connection refused"}},
    {"type": "error", "message": "connection refused"},
])
def test_explicit_codex_failure_payload_trips_provider_breaker(
        monkeypatch, terminal):
    from types import SimpleNamespace
    from pbatch import config, runner

    _enable_jsonl_adapter(monkeypatch, config)
    failures = []
    monkeypatch.setattr(runner, "_pb_register_fail",
                        lambda reason, provider="": failures.append(reason))
    lines = _codex_events("partial")[:-1]
    lines[1:1] = [json.dumps({
        "type": "item.completed", "item": {
            "type": "command_execution", "aggregated_output": "ordinary",
        }}) + "\n" for _index in range(10)]
    lines.append(json.dumps(terminal) + "\n")

    result = runner._result_from_proc(
        Task("review", agent="codex-json"),
        SimpleNamespace(returncode=0), lines, [], 0.1)

    assert result.success is False
    assert terminal["type"] in result.reason
    assert result.stdout == "".join(lines)
    assert failures and "connection refused" in failures[0]


@pytest.mark.parametrize(("stderr", "trips"), [
    ([], False),
    (["connection refused\n"], True),
])
def test_malformed_codex_body_is_not_health_evidence(
        monkeypatch, stderr, trips):
    from types import SimpleNamespace
    from pbatch import config, runner

    _enable_jsonl_adapter(monkeypatch, config)
    failures = []
    monkeypatch.setattr(runner, "_pb_register_fail",
                        lambda reason, provider="": failures.append(reason))
    lines = _codex_events("partial")[:1] + ["connection refused not-json\n"]

    result = runner._result_from_proc(
        Task("review", agent="codex-json"),
        SimpleNamespace(returncode=0), lines, stderr, 0.1)

    assert result.success is False
    assert "malformed" in result.reason
    assert result.stdout == "".join(lines)
    assert bool(failures) is trips


def test_failed_codex_turn_still_establishes_captured_session(
        tmp_path, monkeypatch):
    from pbatch import config
    from pbatch.models import TaskResult
    from pbatch.runner_session import bind_spawned_session
    from pbatch.session import resolve_provider_session

    _enable_jsonl_adapter(monkeypatch, config)
    task = Task("review", agent="codex-json", cwd=str(tmp_path))
    flags = config.agent_session_flags(
        "start", "logical-sid", "work", "codex-json")
    lines = _codex_events("partial", terminal="turn.failed")

    result = bind_spawned_session(
        TaskResult(task, False, reason="turn.failed"), task, flags, lines,
        "work", "logical-sid", str(tmp_path))

    assert result.session_established is True
    provider_id, found = resolve_provider_session(
        str(tmp_path), "work", "logical-sid", "continue", "codex-json")
    assert found and provider_id.startswith("01a0272d")


def test_timed_out_codex_turn_captures_started_thread(tmp_path, monkeypatch):
    from pbatch import config, runner
    from pbatch.process_exec import _DrainTimeout
    from pbatch.session import resolve_provider_session

    _enable_jsonl_adapter(monkeypatch, config)
    task = Task("review", agent="codex-json", cwd=str(tmp_path), timeout=1)
    flags = config.agent_session_flags(
        "start", "logical-sid", "work", "codex-json")

    class Proc:
        pid = 123
        returncode = None

    def timed_out(*_args, **_kwargs):
        raise _DrainTimeout(
            [], 1, _codex_events()[:1], [], (False, False), ())

    monkeypatch.setattr(runner, "_spawn_agent", lambda *_args: Proc())
    monkeypatch.setattr(runner, "_register_proc", lambda _proc: None)
    monkeypatch.setattr(runner, "_unregister_proc", lambda _proc: None)
    monkeypatch.setattr(runner, "_kill_group", lambda _proc: None)
    monkeypatch.setattr(runner, "_stream_task", timed_out)
    monkeypatch.setattr(runner, "_pb_cooldown", lambda _task: None)
    monkeypatch.setattr(runner, "_budget_gate", lambda *_args: None)
    monkeypatch.setattr(runner, "_rate_gate", lambda *_args: None)

    result = runner.run_task(
        task, session_flags=flags, session_name="work")

    assert not result.success and result.reason == "task timed out"
    assert result.session_established is True
    _provider_id, found = resolve_provider_session(
        str(tmp_path), "work", "logical-sid", "continue", "codex-json")
    assert found


def test_prompt_limit_covers_every_session_led_layout(monkeypatch):
    from pbatch import config
    from pbatch.runner_results import prompt_limit_result

    monkeypatch.setattr(config, "PROMPT_MAX_BYTES", 4)
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    monkeypatch.setattr(config, "_AGENTS_CFG", {"codex": {
        "argument_flags": {"prompt": ["exec"]},
        "session_flags": {
            "start": ["exec", "{prompt}"],
            "continue": ["exec", "res", "sid", "{prompt}"],
            "fork": ["exec", "fork", "sid", "{prompt}"],
        },
    }})
    task = Task(prompt="12345", agent="codex")

    for kind in ("start", "continue", "fork"):
        flags = config.agent_session_flags(kind, "sid", "work", "codex")
        result = prompt_limit_result(task, task.to_cmd(flags))
        assert result is not None and "prompt exceeds 4 bytes" in result.reason


def test_task_agent_rebinds_session_flags_and_captures_provider_thread(
        tmp_path, monkeypatch):
    from pbatch import config, runner
    from pbatch.session import provider_session_path, write_provider_session

    agent = "codex-stage"
    thread_id = "01a0272d-66ab-74f1-8891-1013ac2cb87a"
    monkeypatch.setattr(config, "AGENT_BIN", "pi-global")
    monkeypatch.setattr(config, "MEMORY_MODE", "off")
    monkeypatch.setattr(config, "_AGENT_CFG", {
        "argument_flags": {"prompt": "-p"},
        "session_flags": {"continue": ["--session-id", "{session}"]},
    })
    monkeypatch.setattr(config, "_AGENTS_CFG", {agent: {
        "argument_flags": {"prompt": ["exec"]},
        "capture_thread_id": True,
        "session_flags": {
            "continue": ["exec", "resume", "{session}", "{prompt}"],
        },
        "extra_args": ["--json"],
    }})
    name, sid = "review", "logical-sid"
    write_provider_session(str(tmp_path), name, sid, thread_id, agent)
    global_flags = config.agent_session_flags(
        "continue", sid, name, "pi-global")

    cmd, _cwd, resolved_sid, _name = runner._memory_invocation(
        Task(prompt="continue work", agent=agent, model="", cwd=str(tmp_path)),
        global_flags, name)

    assert resolved_sid == sid
    assert cmd == [agent, "exec", "resume", thread_id, "continue work",
                   "--json"]
    runner._capture_codex_thread([
        json.dumps({"type": "thread.started", "thread_id": thread_id}) + "\n",
    ], name, "new-sid", str(tmp_path), agent)
    assert provider_session_path(
        str(tmp_path), name, "new-sid", agent).read_text() == thread_id


def test_provider_session_mapping_is_scoped_by_agent_and_provider(
        tmp_path, monkeypatch):
    from pbatch import config
    from pbatch.session import (provider_session_path,
                                resolve_provider_session,
                                write_provider_session)

    monkeypatch.setattr(config, "AGENT_BIN", "global-agent")
    a_thread = "01a0272d-66ab-74f1-8891-1013ac2cb87a"
    b_thread = "01b0272d-66ab-74f1-8891-1013ac2cb87b"
    write_provider_session(
        str(tmp_path), "work", "sid", a_thread, "agent-a", "cloud-a")
    write_provider_session(
        str(tmp_path), "work", "sid", b_thread, "agent-b", "cloud-b")

    assert resolve_provider_session(
        str(tmp_path), "work", "sid", "continue", "agent-a",
        "cloud-a") == (a_thread, True)
    assert resolve_provider_session(
        str(tmp_path), "work", "sid", "continue", "agent-b",
        "cloud-b") == (b_thread, True)
    assert resolve_provider_session(
        str(tmp_path), "work", "sid", "continue", "agent-a",
        "cloud-b") == ("sid", False)
    unsafe = provider_session_path(
        str(tmp_path), "../../name", "../../sid", "agent-a", "cloud-a")
    assert unsafe.parent == config.data_dir(str(tmp_path)) / "sessions"
    assert "/" not in unsafe.name and ".." not in unsafe.name


def test_provider_session_mapping_does_not_follow_symlinks(tmp_path,
                                                           monkeypatch):
    from pbatch import config
    from pbatch.session import provider_session_path, resolve_provider_session

    monkeypatch.setattr(config, "AGENT_BIN", "global-agent")
    thread_id = "01a0272d-66ab-74f1-8891-1013ac2cb87a"
    target = tmp_path / "outside-thread"
    target.write_text(thread_id, encoding="utf-8")
    mapping = provider_session_path(
        str(tmp_path), "work", "sid", "agent-a", "cloud-a")
    mapping.parent.mkdir(parents=True)
    mapping.symlink_to(target)

    assert resolve_provider_session(
        str(tmp_path), "work", "sid", "continue", "agent-a",
        "cloud-a") == ("sid", False)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_provider_session_mapping_rejects_fifo_without_blocking(
        tmp_path, monkeypatch):
    from pbatch import config
    from pbatch.session import provider_session_path, resolve_provider_session

    monkeypatch.setattr(config, "AGENT_BIN", "global-agent")
    mapping = provider_session_path(
        str(tmp_path), "work", "sid", "agent-a", "cloud-a")
    mapping.parent.mkdir(parents=True)
    os.mkfifo(mapping)

    assert resolve_provider_session(
        str(tmp_path), "work", "sid", "continue", "agent-a",
        "cloud-a") == ("sid", False)


def test_codex_thread_capture_rejects_recursive_json(tmp_path, monkeypatch):
    from pbatch import config
    from pbatch.session import capture_codex_thread

    _enable_jsonl_adapter(monkeypatch, config)
    nested = "[" * 2000 + "0" + "]" * 2000 + "\n"

    assert not capture_codex_thread(
        [nested], "work", "sid", str(tmp_path), "codex-json")


@pytest.mark.parametrize("payload", ["[]\n", "[" * 2000 + "0" + "]" * 2000])
def test_session_compaction_scan_ignores_non_object_and_recursive_json(
        tmp_path, monkeypatch, payload):
    from pbatch import session

    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path)
    folder = tmp_path / session.workdir_key("/work")
    folder.mkdir()
    (folder / "2026_bad.jsonl").write_text(payload, encoding="utf-8")

    assert session.has_compaction("/work", "bad") is False


def test_shared_serial_sessions_are_tracked_per_task_agent(tmp_path, monkeypatch):
    from pbatch import config, runner
    from pbatch.models import TaskResult

    monkeypatch.setattr(config, "AGENT_BIN", "global")
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    monkeypatch.setattr(config, "_AGENTS_CFG", {
        "agent-a": {"session_flags": {
            "start": ["a-start", "{session}"],
            "continue": ["a-next", "{session}"],
        }},
        "agent-b": {"session_flags": {
            "start": ["b-start", "{session}"],
            "continue": ["b-next", "{session}"],
        }},
    })
    observed = []

    def fake_run(task, _validate, _index, _total, _retries,
                 _retry_delay, _backoff, flags, retry_flags, **_kwargs):
        observed.append((task.agent, list(flags or []),
                         list(retry_flags or [])))
        return TaskResult(task, True, stdout="ok", returncode=0)

    monkeypatch.setattr(runner, "_run_retried", fake_run)
    tasks = [
        Task("one", agent="agent-a", cwd=str(tmp_path)),
        Task("two", agent="agent-b", cwd=str(tmp_path)),
        Task("three", agent="agent-a", cwd=str(tmp_path)),
    ]

    results = runner.run_serial(
        tasks, session_mode="shared", session_id="sid", session_name="work")

    assert all(result.success for result in results)
    assert observed == [
        ("agent-a", ["a-start", "sid"], ["a-start", "sid"]),
        ("agent-b", ["b-start", "sid"], ["b-start", "sid"]),
        ("agent-a", ["a-next", "sid"], ["a-next", "sid"]),
    ]


def test_failed_pre_spawn_task_does_not_activate_shared_session(
        tmp_path, monkeypatch):
    from pbatch import config, runner
    from pbatch.models import TaskResult

    monkeypatch.setattr(config, "_AGENTS_CFG", {"agent-a": {
        "session_flags": {
            "start": ["start", "{session}"],
            "continue": ["next", "{session}"],
        },
    }})
    observed = []

    def fake_run(task, _validate, _index, _total, _retries,
                 _retry_delay, _backoff, flags, retry_flags, **_kwargs):
        observed.append((list(flags or []), list(retry_flags or [])))
        success = len(observed) > 1
        return TaskResult(task, success, stdout="ok" if success else "",
                          reason="prompt exceeds limit" if not success else "")

    monkeypatch.setattr(runner, "_run_retried", fake_run)
    state = set()
    tasks = [Task("one", agent="agent-a", cwd=str(tmp_path)),
             Task("two", agent="agent-a", cwd=str(tmp_path))]

    runner.run_serial(
        tasks, session_mode="shared", session_id="sid",
        session_name="work", session_state=state)

    assert observed == [
        (["start", "sid"], ["start", "sid"]),
        (["start", "sid"], ["start", "sid"]),
    ]


def test_post_spawn_failure_activates_following_shared_task(
        tmp_path, monkeypatch):
    from pbatch import config, runner
    from pbatch.models import TaskResult

    monkeypatch.setattr(config, "_AGENTS_CFG", {"agent-a": {
        "session_flags": {
            "start": ["start", "{session}"],
            "continue": ["next", "{session}"],
        },
    }})
    observed = []

    def fake_run(task, _validate, _index, _total, _retries,
                 _retry_delay, _backoff, flags, _retry_flags, **_kwargs):
        observed.append(list(flags or []))
        return TaskResult(
            task, len(observed) > 1, reason="agent exited 1",
            session_established=True)

    monkeypatch.setattr(runner, "_run_retried", fake_run)
    tasks = [Task("one", agent="agent-a", cwd=str(tmp_path)),
             Task("two", agent="agent-a", cwd=str(tmp_path))]

    runner.run_serial(
        tasks, session_mode="shared", session_id="sid", session_name="work")

    assert observed == [["start", "sid"], ["next", "sid"]]


@pytest.mark.parametrize(("established", "expected"), [
    (False, ["start", "start"]),
    (True, ["start", "continue"]),
])
def test_retry_session_choice_depends_on_process_spawn(
        tmp_path, monkeypatch, established, expected):
    from pbatch import config, runner
    from pbatch.models import TaskResult

    monkeypatch.setattr(config, "_AGENTS_CFG", {"agent-a": {
        "session_flags": {
            "start": ["start", "{session}"],
            "continue": ["continue", "{session}"],
        },
    }})
    observed = []

    def fake_gate(task, _validate, _index, _total, session_flags=None,
                  _parallel=False, _name="", **_kwargs):
        observed.append(getattr(session_flags, "kind", ""))
        first = len(observed) == 1
        return TaskResult(
            task, not first, stdout="ok" if not first else "",
            reason="network timeout" if first else "",
            session_established=established if first else True)

    monkeypatch.setattr(runner, "_run_with_gate", fake_gate)
    monkeypatch.setattr(runner, "_scheduled_sleep", lambda _seconds: None)

    result = runner.run_serial(
        [Task("one", agent="agent-a", cwd=str(tmp_path))], retries=1,
        session_mode="shared", session_id="sid", session_name="work")[0]

    assert result.success
    assert observed == expected


def test_missing_native_mapping_rejects_resume_before_spawn(
        tmp_path, monkeypatch):
    from pbatch import config, runner

    monkeypatch.setattr(config, "MEMORY_MODE", "off")
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    monkeypatch.setattr(config, "_AGENTS_CFG", {"codex": {
        "argument_flags": {"prompt": ["exec"]},
        "capture_thread_id": True,
        "session_flags": {
            "continue": ["exec", "res", "{session}", "{prompt}"],
        },
    }})
    flags = config.agent_session_flags(
        "continue", "logical-sid", "work", "codex")

    result = runner._task_invocation(
        Task("continue", agent="codex", cwd=str(tmp_path)), flags, "work")

    assert not result.success
    assert "session mapping is missing" in result.reason


def test_shared_pipeline_resumes_native_thread_across_stages(
        tmp_path, monkeypatch):
    from pbatch import config
    from pbatch.models import Pipeline
    from pbatch.pipeline import run_pipeline

    thread_id = "01a0272d-66ab-74f1-8891-1013ac2cb87a"
    args_log = tmp_path / "args.jsonl"
    agent = tmp_path / "codex-like.py"
    agent.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"log = {str(args_log)!r}\n"
        "with open(log, 'a', encoding='utf-8') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"print(json.dumps({{'type': 'thread.started', "
        f"'thread_id': {thread_id!r}}}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': {\n"
        "    'type': 'agent_message', 'text': 'artifact'}}))\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}))\n",
        encoding="utf-8")
    agent.chmod(0o755)
    monkeypatch.setattr(config, "MEMORY_MODE", "off")
    monkeypatch.setattr(config, "AGENT_DEFAULT_MODEL", "")
    monkeypatch.setattr(config, "_AGENT_CFG", {})
    monkeypatch.setattr(config, "_AGENTS_CFG", {str(agent): {
        "argument_flags": {"prompt": ["exec"]},
        "capture_thread_id": True,
        "session_flags": {
            "start": ["exec", "{prompt}"],
            "continue": ["exec", "res", "{session}", "{prompt}"],
        },
        "extra_args": ["--json"],
    }})
    pipeline = Pipeline(stages=[
        Stage(name="one", agent=str(agent), from_prompt="first",
              output=str(tmp_path / "one.md"), cwd=str(tmp_path)),
        Stage(name="two", agent=str(agent), from_prompt="second",
              output=str(tmp_path / "two.md"), cwd=str(tmp_path)),
    ])

    _results, failed = run_pipeline(
        pipeline, session_mode="shared", session_name="work")

    assert failed == []
    calls = [json.loads(line) for line in args_log.read_text().splitlines()]
    assert calls == [
        ["exec", "first", "--json"],
        ["exec", "res", thread_id, "second", "--json"],
    ]
    assert (tmp_path / "one.md").read_text(encoding="utf-8") == "artifact"
    assert (tmp_path / "two.md").read_text(encoding="utf-8") == "artifact"


def test_ledger_records_stage_agent(tmp_path):
    from pbatch import agent_ledger
    import pbatch.config as config_mod
    original = config_mod.AGENT_LEDGER_ENABLED
    config_mod.AGENT_LEDGER_ENABLED = True
    try:
        result = type("R", (), {
            "run_id": "run-1", "session_id": "", "session_name": "",
            "elapsed": 1.5, "returncode": 0,
        })()
        task = Task(prompt="p", agent="/tmp/special-agent",
                    output=str(tmp_path / "r.md"))
        ok = agent_ledger.record_result(result, task, str(tmp_path))
        assert ok
        rows = agent_ledger.query(config_mod.data_dir(str(tmp_path)))
        assert rows and rows[-1]["agent"] == "/tmp/special-agent"
    finally:
        config_mod.AGENT_LEDGER_ENABLED = original


def test_pipeline_schema_allows_stage_and_task_agent():
    from pbatch.pipeline_schema import validate_pipeline
    good = {"stages": [
        {"name": "s1", "agent": "codex", "from_prompt": "x",
         "output": "a.md"},
        {"name": "s2", "from_outputs": "s1",
         "tasks": [{"prompt": "y", "output": "b.md", "agent": "pi"}]},
    ]}
    errors = validate_pipeline(good)
    assert not errors, errors


def test_end_to_end_stages_use_their_own_binaries(tmp_path):
    """CLI-level: two stages with different --agent style executors; each
    stage's artifact carries its agent's marker."""
    a = _agent(tmp_path, "STAGE_A", "agent-a.sh")
    b = _agent(tmp_path, "STAGE_B", "agent-b.sh")
    pipe = tmp_path / "pipe.yaml"
    pipe.write_text(f"""
stages:
  - name: first
    agent: {a}
    from_prompt: "task one"
    output: {tmp_path / 'first.md'}
  - name: second
    agent: {b}
    from_prompt: "task two"
    output: {tmp_path / 'second.md'}
""", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "pi-batch.py"),
         str(pipe), "--memory-mode", "off", "--no-lock"],
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-800:]
    first = (tmp_path / "first.md").read_text(encoding="utf-8", errors="replace")
    second = (tmp_path / "second.md").read_text(encoding="utf-8", errors="replace")
    assert "STAGE_A" in first
    assert "STAGE_B" in second
