import os
import threading
import time

import pytest

from pbatch.hub_gateway_files import bounded_capture, _CaptureStop
from tests.test_gateway_file_delivery import owner


def slow_script(tmp_path):
    script = tmp_path / 'owned-child.py'
    marker = tmp_path / 'child.pid'
    script.write_text("import os, time\nfrom pathlib import Path\n"
                      f"Path({str(marker)!r}).write_text(str(os.getpid()))\n"
                      "time.sleep(60)\n")
    return script, marker


def assert_process_reaped(marker):
    assert marker.exists(), 'test child never started'
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def test_whole_capture_deadline_reaps_owned_child(owner, tmp_path, monkeypatch):
    gateway, _, _, _ = owner
    script, marker = slow_script(tmp_path)
    monkeypatch.setattr('pbatch.hub_gateway_files._capture_script', lambda: script)
    monkeypatch.setattr('pbatch.hub_gateway_files.CAPTURE_SECONDS', 0.3)
    start = time.monotonic()
    result = bounded_capture(gateway, gateway._files.identity)
    assert time.monotonic() - start < 2
    assert result['unavailable'][0]['code'] == 'read_timeout'
    assert_process_reaped(marker)


def test_stopping_gateway_reaps_inflight_owned_capture(owner, tmp_path, monkeypatch):
    gateway, _, _, _ = owner
    script, marker = slow_script(tmp_path)
    monkeypatch.setattr('pbatch.hub_gateway_files._capture_script', lambda: script)
    result = []
    thread = threading.Thread(target=lambda: result.append(bounded_capture(gateway, gateway._files.identity)))
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        gateway._stop.set()
        thread.join(2)
        assert not thread.is_alive()
        assert result[0]['unavailable'][0]['code'] == 'capture_unavailable'
        assert_process_reaped(marker)
    finally:
        gateway._stop.set()
        thread.join(2)


def test_turn_cancel_prevents_delayed_capture_from_starting(owner, monkeypatch):
    gateway, _, session, _ = owner
    managed = gateway._managed(session.local_id)
    managed.active_turn = 'cancelled-turn'
    managed.cancel_requested = True
    cancel = _CaptureStop(gateway, managed, 'cancelled-turn')
    monkeypatch.setattr('pbatch.process_exec.spawn_registered',
                        lambda *args: pytest.fail('cancelled capture started a process'))
    result = bounded_capture(gateway, gateway._files.identity, cancel)
    assert result['files'] == []
    assert result['unavailable'][0]['code'] == 'capture_unavailable'
