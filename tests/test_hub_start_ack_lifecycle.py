"""A persisted running acknowledgement is not evidence that the provider has started."""
import threading

from pbatch.hub_client import HubClient
from test_hub_integration import fleet, _until


def test_hub_running_can_precede_provider_start_while_acknowledgement_is_in_flight(fleet):
    user, create, _ = fleet
    acknowledged, release = threading.Event(), threading.Event()
    errors = []

    class AckBarrier(HubClient):
        def send_event(self, event, **kwargs):
            response = super().send_event(event, **kwargs)
            if event['kind'] == 'turn.running':
                acknowledged.set()
                assert release.wait(3), 'fixture did not release its own acknowledgement'
            return response

    gateway, runtime, session = create('alpha', client_type=AckBarrier, hold=True)
    turn = user.send_prompt(session, 'inspect synthetic project', 'ack-order')['turn_id']
    def poll():
        try:
            gateway.run_once()
        except BaseException as exc:
            errors.append(type(exc).__name__)
    worker = threading.Thread(target=poll)
    worker.start()
    try:
        assert acknowledged.wait(2)
        assert user.get_session(session)['status'] == 'running'
        assert user.get_turn(turn)['state'] == 'running'
        assert any(event['kind'] == 'turn.running' for event in user.session_events(session))
        assert runtime.prompts == [] and runtime.handle is None
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
    _until([gateway], lambda: runtime.handle is not None)
    assert runtime.prompts == ['inspect synthetic project']
