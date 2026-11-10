"""Real HTTP/JWT/SQLite observation; synthetic identity, no provider or live project."""
from dataclasses import replace
import json
import threading

import pytest

from pbatch.hub_auth import GATEWAY_SCOPE, HubPrincipal
from pbatch.hub_client import HubClient, HubError
from pbatch.hub_gateway import HubGateway, HubGatewayConfig
from pbatch.hub_http import HubHTTPServer
from pbatch.hub_store import HubStore
from test_hub_auth import signed


@pytest.fixture
def observation_hub(tmp_path, signed):
    auth, token, *_ = signed
    auth.grants = [replace(auth.grants[0], scopes=frozenset({
        'agent.instances:read', 'agent.sessions:read', 'agent.sessions:write'})),
        HubPrincipal('team', 'machine', 'machine', 'gateway', frozenset({'alpha'}),
                     frozenset({GATEWAY_SCOPE}))]
    clock = [1000.0]
    store = HubStore(tmp_path / 'hub.sqlite3', clock=lambda: clock[0])
    server = HubHTTPServer(('127.0.0.1', 0), store, auth)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    project = tmp_path / 'project'
    project.mkdir()
    config = HubGatewayConfig(origin, cwd=str(project), project_id='abr', instance_id='alpha',
                              state_dir=str(tmp_path / 'observer'), observe_only=True,
                              sessions=0, max_sessions=0)
    machine = HubClient(origin, token(sub='machine', client_id='machine', scope=GATEWAY_SCOPE))
    user = HubClient(origin, token(scope='agent.instances:read agent.sessions:read agent.sessions:write'))
    try:
        yield config, machine, user, token, clock, store
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()


def no_provider(_options):
    pytest.fail('observation attempted to create an AgentSession')


def test_real_authenticated_instance_is_visible_but_not_session_capacity(observation_hub):
    config, machine, user, token, _, store = observation_hub
    with HubGateway(config, client=machine, session_factory=no_provider) as gateway:
        gateway.run_once()
        instances = user.list_instances()
        assert len(instances) == 1 and instances[0]['instance_id'] == 'alpha'
        assert instances[0]['project_id'] == 'abr' and instances[0]['online'] is True
        assert instances[0]['session_capacity'] == 0
        assert user.list_sessions() == []
        assert config.cwd not in json.dumps(instances)
        with pytest.raises(HubError) as rejected:
            user.create_session('alpha', 'not-allowed', idempotency_key='observe-create')
        assert rejected.value.status in (403, 409)
        assert store.sessions('team') == []
        stranger = HubClient(config.hub_url, token(sub='other-person', scope='agent.instances:read'))
        with pytest.raises(HubError) as rejected:
            stranger.list_instances()
        assert rejected.value.status == 403


def test_observer_still_obeys_live_ownership_and_generation_fencing(observation_hub, tmp_path):
    config, machine, _, _, clock, _ = observation_hub
    first = HubGateway(config, client=machine, session_factory=no_provider)
    first.start()
    second = None
    try:
        alternative = replace(config, state_dir=str(tmp_path / 'second-observer'))
        with pytest.raises(HubError) as blocked:
            HubGateway(alternative, client=machine, session_factory=no_provider)
        assert blocked.value.status == 409
        clock[0] += 61  # advance the synthetic Hub clock, never evict a live production owner
        second = HubGateway(alternative, client=machine, session_factory=no_provider)
        second.start()
        assert second.generation > first.generation
        first._last_heartbeat = 0
        with pytest.raises(RuntimeError, match='fencing'):
            first.run_once()
        assert first._stop.is_set() and not first._turn_threads
    finally:
        first.close()
        if second is not None:
            second.close()
