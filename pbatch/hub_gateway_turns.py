"""Turn launch and unwind synchronized with Gateway session lifecycle changes."""

from __future__ import annotations

import threading

from .hub_gateway_protocol import has_terminal_event


def launch_turn(gateway, turn_id, prompt, managed, hub_session_id):
    with gateway._session_lock:
        gateway._pending_starts.pop(turn_id, None)
        if not gateway._session_available(managed) or managed.active_turn:
            return
        gateway._store.set_turn(turn_id, "starting")
        managed.active_turn = turn_id
        thread = threading.Thread(target=gateway._run_turn,
                                  args=(turn_id, prompt, managed),
                                  name=f"hub-turn-{turn_id[:12]}", daemon=True)
        gateway._turn_threads[turn_id] = thread
        try:
            thread.start()
        except RuntimeError as exc:
            gateway._turn_threads.pop(turn_id, None)
            managed.active_turn = ""
            gateway._publish_synthetic(turn_id, hub_session_id, "turn.failed",
                                       {"reason": type(exc).__name__})


def run_turn(gateway, turn_id, prompt, managed):
    try:
        with gateway._session_lock:
            if not gateway._session_available(managed):
                return
            gateway._store.set_turn(turn_id, "executing")
        from .hub_compute_broker import turn_tools
        with turn_tools(gateway, managed, turn_id, prompt) as tool_prompt:
            result = managed.session.prompt(tool_prompt)
        if not has_terminal_event(result):
            gateway._publish_synthetic(turn_id, managed.hub_id, "turn.failed",
                                       {"reason": "runtime_missing_terminal_event"})
    except Exception as exc:
        gateway._publish_synthetic(turn_id, managed.hub_id, "turn.failed",
                                   {"reason": type(exc).__name__})
    finally:
        with gateway._session_lock:
            managed.active_turn = ""
            gateway._turn_threads.pop(turn_id, None)
