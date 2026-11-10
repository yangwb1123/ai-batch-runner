"""Turn launch and unwind synchronized with Gateway session lifecycle changes."""

from __future__ import annotations

import threading

from .hub_gateway_protocol import TERMINAL


def launch_turn(gateway, turn_id, prompt, managed, hub_session_id):
    with gateway._session_lock:
        gateway._pending_starts.pop(turn_id, None)
        if not gateway._session_available(managed) or managed.active_turn:
            return
        gateway._store.set_turn(turn_id, "starting")
        managed.active_turn = turn_id
        managed.terminal_event_id = ""
        managed.cancel_requested = False
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
    completed = False
    try:
        with gateway._session_lock:
            if not gateway._session_available(managed):
                return
            gateway._store.set_turn(turn_id, "executing")
        from .hub_compute_broker import turn_tools
        terminal = False
        with turn_tools(gateway, managed, turn_id, prompt) as tool_prompt:
            # AgentSession owns exact session/run/turn filtering, including
            # terminal-only streams and events from same-turn child actors.
            stream = managed.session.prompt_events(tool_prompt)
            try:
                for event in stream:
                    accepted = gateway._on_event(managed, event, turn_id)
                    terminal = accepted and event.kind in TERMINAL
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
        if not terminal:
            gateway._publish_synthetic(turn_id, managed.hub_id, "turn.failed",
                                       {"reason": "runtime_missing_terminal_event"})
        else:
            completed = event.kind == "turn.completed"
    except Exception as exc:
        gateway._publish_synthetic(turn_id, managed.hub_id, "turn.failed",
                                   {"reason": type(exc).__name__})
    finally:
        gateway._files.capture(managed, turn_id, completed)
        with gateway._session_lock:
            managed.active_turn = ""
            gateway._turn_threads.pop(turn_id, None)
