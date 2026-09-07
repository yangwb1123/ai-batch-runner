from types import SimpleNamespace

from pbatch.tui_actor_controls import TUIActorControlMixin
from pbatch.tui_actor_tree import ActorNode, ActorTreeSnapshot
from pbatch.tui_native import NativePipelineControlChannel, NativePipelineObserver
from pbatch.tui_protocol import RuntimeCapabilities
from pbatch.tui_runtime import RuntimeInfo


class _Runtime:
    def __init__(self, enabled=True):
        self.calls = []
        self._info = RuntimeInfo(
            "fake", 1, RuntimeCapabilities(
                runtime="fake", capabilities={"actor_control": enabled}))

    def runtime_info(self):
        return self._info

    def steer_actor(self, request):
        self.calls.append(("steer", request.turn_id, request.actor_id, request.text))

    def interrupt_actor(self, request):
        self.calls.append(("stop", request.turn_id, request.actor_id))


class _Harness(TUIActorControlMixin):
    def __init__(self, runtime):
        self._busy = True
        self._review_busy = False
        self._pipeline_busy = False
        self._actor_control_pending = False
        self._adapter = runtime
        self._last_status = ""
        self.notifications = []
        self.status_refreshes = 0
        self.runtime_view = SimpleNamespace(
            turn_id="turn-1",
            actors=ActorTreeSnapshot(nodes=(ActorNode(
                actor_id="child-1", parent_actor_id="agent_main",
                current=True, status="RUNNING"),), turn_id="turn-1"))
        self._runtime_view = self.runtime_view

    def _usage(self, command, suffix):
        self.notifications.append(("usage", command, suffix))

    def notify(self, text, severity="information"):
        self.notifications.append((severity, text))

    def _refresh_status(self):
        self.status_refreshes += 1

    def run_worker(self, worker, **_kwargs):
        worker()

    def call_from_thread(self, callback, *args):
        callback(*args)


def test_actor_controls_require_typed_current_identity_and_dispatch_exact_requests():
    runtime = _Runtime()
    harness = _Harness(runtime)

    harness._slash_actor_steer(("child-1", "focus", "tests"))
    harness._slash_actor_stop(("child-1",))

    assert runtime.calls == [
        ("steer", "turn-1", "child-1", "focus tests"),
        ("stop", "turn-1", "child-1"),
    ]
    assert harness._actor_control_pending is False
    assert harness._last_status == "Actor stop accepted"


def test_actor_controls_fail_closed_for_unknown_actor_or_capability():
    runtime = _Runtime(enabled=False)
    harness = _Harness(runtime)

    harness._slash_actor_stop(("missing",))
    harness._slash_actor_steer(("child-1", "try"))

    assert runtime.calls == []
    assert any("not an active typed actor" in item[1]
               for item in harness.notifications if item[0] == "warning")
    assert any("failed" in item[1]
               for item in harness.notifications if item[0] == "error")


def test_native_pipeline_actor_stop_targets_one_registered_task():
    channel = NativePipelineControlChannel()
    observer = NativePipelineObserver(
        lambda _event: None, "/tmp/project", session_id="session",
        run_id="run", turn_id="turn", actor_id="pipeline")
    channel.bind(observer)
    observer.bind_approval_channel(channel)
    observer.start("demo", 1)
    observer.stage_started("build", 1, 1)
    task = SimpleNamespace(effective_agent=lambda: "pi", output="out.md")
    actor_id = observer.task_started("build", 1, 1, task)
    assert channel.register_task(actor_id, task)
    harness = _Harness(_Runtime())
    harness._pipeline_busy = True
    harness._native_pipeline_approval = channel
    harness._runtime_view = SimpleNamespace(
        workspace_id="/tmp/project", session_id="session", run_id="run",
        turn_id="turn", actor_id="pipeline",
        actors=ActorTreeSnapshot(nodes=(ActorNode(
            actor_id=actor_id, parent_actor_id="stage_1_build",
            current=True, status="RUNNING"),), turn_id="turn"))

    harness._slash_actor_stop((actor_id,))
    harness._slash_actor_steer((actor_id, "not supported"))

    assert channel.cancel_event_for_task(task).is_set()
    assert any("accepted" in item[1]
               for item in harness.notifications if item[0] == "information")
    assert any("do not support steering" in item[1]
               for item in harness.notifications if item[0] == "warning")

    harness._runtime_view = SimpleNamespace(
        workspace_id="/tmp/project", session_id="session", run_id="run",
        turn_id="turn", actor_id="pipeline",
        actors=ActorTreeSnapshot(nodes=(ActorNode(
            actor_id=actor_id, parent_actor_id="stage_1_build",
            current=False, terminal=True, status="CANCELLED"),),
            turn_id="turn"))
    harness._slash_actor_stop((actor_id,))
    assert any("not an active typed actor" in item[1]
               for item in harness.notifications if item[0] == "warning")


def test_actor_control_command_arguments_are_bounded_and_validated():
    runtime = _Runtime()
    harness = _Harness(runtime)

    harness._slash_actor_stop(())
    harness._slash_actor_steer(("child-1",))
    harness._slash_actor_steer(("child-1", "x" * 10_000))

    assert harness.notifications[0] == ("usage", "actor-stop", "<actor-id>")
    assert harness.notifications[1] == ("usage", "actor-steer", "<actor-id> <text>")
    assert len(runtime.calls) == 1
    assert len(runtime.calls[0][3].encode("utf-8")) == 4096
