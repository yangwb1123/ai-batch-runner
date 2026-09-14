import asyncio
import threading

from textual.widgets import Button, Select, Static

from pbatch.hub_client import HubError
from pbatch.hub_compute_tui import ComputeTUI, MAX_TASK_OUTPUT_CHARS


class _Hub:
    def list_sessions(self):
        return [
            {"session_id": "s1", "name": "first", "project_id": "p1",
             "status": "idle", "controllable": True},
            {"session_id": "s2", "name": "second", "project_id": "p2",
             "status": "idle", "controllable": True},
        ]


class _Compute:
    def __init__(self):
        self.client = _Hub()
        self.submissions = []
        self.cancellations = []
        self.tasks_by_session = {"s1": [], "s2": []}
        self.block_project = ""
        self.device_started = threading.Event()
        self.release_device = threading.Event()
        self.fail_devices = False
        self.fail_submit_once = False

    def devices(self, project_id=""):
        if project_id == self.block_project:
            self.device_started.set()
            self.release_device.wait(2)
        if self.fail_devices:
            raise HubError("missing scope", code="scope_required", status=403)
        return [{"device_id": f"{project_id}-gpu", "name": "GPU node",
                 "schedulable": True, "cpu_cores": 8,
                 "reserved_cpu_cores": 2, "available_memory_bytes": 8 * 2**30,
                 "total_memory_bytes": 16 * 2**30}]

    def tasks(self, session_id=""):
        return list(self.tasks_by_session.get(session_id, []))

    def get(self, task_id):
        for rows in self.tasks_by_session.values():
            for row in rows:
                if row["task_id"] == task_id:
                    return row
        raise HubError("task not found", status=404)

    def submit(self, session_id, body, key):
        self.submissions.append((session_id, body.copy(), key))
        if self.fail_submit_once:
            self.fail_submit_once = False
            raise HubError("ack lost", code="hub_unavailable")
        task = {"task_id": "task-1", "session_id": session_id,
                "state": "queued", "archive_state": "pending",
                "argv": body["argv"], "workdir": body["workdir"],
                "result": {}}
        self.tasks_by_session[session_id] = [task]
        return {"task_id": task["task_id"]}

    def cancel(self, task_id):
        self.cancellations.append(task_id)
        task = self.get(task_id)
        task["state"] = "cancel_requested"
        return {"state": task["state"]}


def test_compute_tui_lists_sessions_devices_and_submits_structured_request():
    async def scenario():
        client = _Compute()
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            assert app._selected_session == "s1"
            assert "p1-gpu" in app.query_one("#devices", Static).render().plain
            assert not app.query_one("#submit", Button).disabled
            assert app.query_one("#target", Select).value == ""

            app.query_one("#argv").value = '["python", "job.py", "--fast"]'
            app.query_one("#workdir").value = "jobs"
            app.query_one("#timeout").value = "120"
            app.query_one("#cpu-cores").value = "4"
            app.query_one("#memory-mib").value = "512"
            app.query_one("#target", Select).value = "p1-gpu"
            await app._submit_task()

            session_id, body, key = client.submissions[0]
            assert session_id == "s1" and key
            assert body == {
                "argv": ["python", "job.py", "--fast"], "workdir": "jobs",
                "timeout": 120, "resources": {
                    "cpu_cores": 4, "memory_bytes": 512 * 1048576},
                "target_device_id": "p1-gpu",
            }
            assert app._selected_task == "task-1"
            assert app.query_one("#tabs").active == "tasks-pane"
            await pilot.pause()

    asyncio.run(scenario())


def test_gpu_form_validates_per_card_memory_and_preserves_retry_key():
    async def scenario():
        client = _Compute()
        app = ComputeTUI(client)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.1)
            app.query_one("#gpu-memory-mib").value = "8192"
            await app._submit_task()
            assert not client.submissions
            assert "positive gpu_count" in app.query_one("#task-status", Static).render().plain
            app.query_one("#gpu-count").value = "2"
            client.fail_submit_once = True
            await app._submit_task()
            await app._submit_task()
            first, retried = client.submissions
            assert first[1]["resources"] == {
                "cpu_cores": 1, "memory_bytes": 0, "gpu_count": 2,
                "gpu_memory_bytes": 8192 * 1048576}
            assert first[1:] == retried[1:]

    asyncio.run(scenario())


def test_gpu_inventory_and_assigned_uuid_are_visible():
    async def scenario():
        client = _Compute()
        original_devices = client.devices
        uuid = "GPU-00000000-0000-0000-0000-000000000001"

        def devices(project_id=""):
            rows = original_devices(project_id)
            rows[0].update(gpu_status="available", gpus=[{
                "uuid": uuid, "schedulable": True, "reserved": False,
                "available_memory_bytes": 8192 * 1048576}])
            return rows

        client.devices = devices
        client.tasks_by_session["s1"] = [{
            "task_id": "gpu-task", "session_id": "s1", "state": "running",
            "gpu_assignment": {"vendor": "nvidia", "mode": "physical", "uuids": [uuid]}}]
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            assert "1 GPU free" in app.query_one("#devices", Static).render().plain
            assert uuid in app.query_one("#task-detail", Static).render().plain

    asyncio.run(scenario())


def test_compute_tui_task_detail_truncates_output_and_shows_archive_state():
    async def scenario():
        client = _Compute()
        task = {"task_id": "task-9", "session_id": "s1", "state": "completed",
                "archive_state": "archived", "artifact_ref": {"key": "prefix/k"},
                "result": {"stdout": "x" * (MAX_TASK_OUTPUT_CHARS + 80),
                           "stderr": "", "exit_code": 0}}
        client.tasks_by_session["s1"] = [task]
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            detail = app.query_one("#task-detail", Static).render().plain
            assert '"archive_state": "archived"' in detail
            assert '"key": "prefix/k"' in detail
            assert '"stdout_truncated": true' in detail
            assert len(detail) < 14000
            assert app.query_one("#cancel-task", Button).disabled

    asyncio.run(scenario())


def test_compute_tui_cancel_updates_selected_task():
    async def scenario():
        client = _Compute()
        client.tasks_by_session["s1"] = [{"task_id": "task-2", "session_id": "s1",
                                           "state": "running", "result": {}}]
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            assert app._selected_task == "task-2"
            await app._cancel_task()
            assert client.cancellations == ["task-2"]
            assert app._tasks["task-2"]["state"] == "cancel_requested"
            assert "Cancellation requested" in app.query_one(
                "#task-status", Static).render().plain

    asyncio.run(scenario())


def test_compute_tui_reuses_idempotency_key_when_acknowledgement_is_lost():
    async def scenario():
        client = _Compute()
        client.fail_submit_once = True
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            await app._submit_task()
            assert "unconfirmed" in app.query_one("#task-status", Static).render().plain
            await app._submit_task()
            assert len(client.submissions) == 2
            assert client.submissions[0][2]
            assert client.submissions[0][2] == client.submissions[1][2]

    asyncio.run(scenario())


def test_compute_tui_reports_missing_device_scope_and_blocks_submit():
    async def scenario():
        client = _Compute()
        client.fail_devices = True
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            message = app.query_one("#devices", Static).render().plain
            assert "agent.devices:read" in message
            assert app.query_one("#submit", Button).disabled
            assert client.submissions == []

    asyncio.run(scenario())


def test_compute_tui_rejects_invalid_structured_input_before_submission():
    async def scenario():
        client = _Compute()
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            app.query_one("#argv").value = "not json"
            await app._submit_task()
            assert client.submissions == []
            assert "valid JSON" in app.query_one(
                "#task-status", Static).render().plain

            app.query_one("#argv").value = '["python"]'
            app.query_one("#workdir").value = "../outside"
            await app._submit_task()
            assert client.submissions == []
            assert "beneath the project root" in app.query_one(
                "#task-status", Static).render().plain

    asyncio.run(scenario())


def test_compute_tui_discards_stale_device_response_after_session_switch():
    async def scenario():
        client = _Compute()
        app = ComputeTUI(client)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            client.block_project = "p1"
            old_request = asyncio.create_task(app._refresh_devices())
            assert await asyncio.to_thread(client.device_started.wait, 1)
            app._selected_session = "s2"
            await app._refresh_devices()
            client.release_device.set()
            await old_request
            rendered = app.query_one("#devices", Static).render().plain
            assert "p2-gpu" in rendered
            assert "p1-gpu" not in rendered
            await pilot.pause(0.05)

    asyncio.run(scenario())
