"""Observation is a local execution restriction, not an automatically granted identity."""
from dataclasses import replace
from pathlib import Path

import pytest

from pbatch import hub_cli
from pbatch.hub_gateway import HubGateway, HubGatewayConfig
from pbatch.hub_gateway_config import normalize_config
from pbatch.hub_gateway_store import GatewayStore
from test_hub_gateway import _FakeClient


class ObservationClient(_FakeClient):
    base_url = 'http://127.0.0.1:8788'

    def __init__(self):
        super().__init__()
        self.calls = []

    def register_instance(self, **values):
        self.calls.append(('register', values))
        return super().register_instance(**values)

    def heartbeat(self, **values):
        self.calls.append(('heartbeat', values))
        return super().heartbeat(**values)

    def get_commands(self, **values):
        pytest.fail('observation cannot retrieve prompts')

    def get_session_requests(self, **values):
        pytest.fail('observation cannot poll creation requests')

    def get_session_close_requests(self, **values):
        pytest.fail('observation cannot poll cancellation/close requests')

    def send_event(self, *args, **values):
        pytest.fail('observation cannot replay execution outbox')


def options(tmp_path):
    project = tmp_path / 'project'
    project.mkdir(exist_ok=True)
    return HubGatewayConfig(
        ObservationClient.base_url, cwd=str(project), state_dir=str(tmp_path / 'state'),
        instance_id='alpha', project_id='abr', name='read-only observation',
        sessions=0, max_sessions=0, observe_only=True)


def no_session(_options):
    pytest.fail('no AgentSession/provider may be constructed in observation mode')


def test_observation_only_registers_and_heartbeats(tmp_path):
    config, client = options(tmp_path), ObservationClient()
    marker = Path(config.cwd) / 'untouched'
    marker.write_bytes(b'local project content')
    with HubGateway(config, client=client, session_factory=no_session) as gateway:
        gateway.run_once()
        assert gateway._creation._thread is None and gateway._closing._thread is None
        assert gateway._store.sessions() == []
        assert gateway._turn_threads == {} and gateway._pending_starts == {}
        assert [kind for kind, _ in client.calls] == ['register', 'heartbeat']
        registered = client.calls[0][1]
        assert registered['session_capacity'] == 0 and registered['session_close_supported'] is False
        for action in [lambda: gateway._create_session('forbidden'),
                       lambda: gateway._dispatch_command({'prompt': 'untrusted'})]:
            with pytest.raises(ValueError, match='observation'):
                action()
    with pytest.raises(RuntimeError, match='stopped'):
        gateway.run_once()
    assert [kind for kind, _ in client.calls] == ['register', 'heartbeat']
    assert marker.read_bytes() == b'local project content'
    assert set(Path(config.cwd).iterdir()) == {marker}


@pytest.mark.parametrize('field,value', [
    ('sessions', 1), ('max_sessions', 1), ('compute_tools', True), ('observe_only', 'yes'),
    ('cwd', ''), ('instance_id', ''), ('project_id', ''), ('state_dir', ''), ('hub_url', ''),
    ('heartbeat_interval', True), ('heartbeat_interval', float('inf')), ('poll_interval', '1'),
    ('poll_interval', float('nan')),
])
def test_incompatible_observation_configuration_fails_before_state(tmp_path, field, value):
    config = replace(options(tmp_path), **{field: value})
    client = ObservationClient()
    with pytest.raises(ValueError):
        HubGateway(config, client=client, session_factory=no_session)
    assert not client.calls and not (tmp_path / 'state').exists()


def test_observation_requires_external_state_and_existing_fixed_workspace(tmp_path):
    config = options(tmp_path)
    for invalid in [replace(config, state_dir=config.cwd),
                    replace(config, state_dir=str(Path(config.cwd) / 'state')),
                    replace(config, cwd=str(tmp_path / 'missing'))]:
        with pytest.raises(ValueError):
            normalize_config(invalid)


def test_observation_state_cannot_change_mode_or_project_or_endpoint(tmp_path):
    config = options(tmp_path)
    with HubGateway(config, client=ObservationClient(), session_factory=no_session):
        pass
    for altered in [replace(config, observe_only=False, sessions=1, max_sessions=8),
                    replace(config, project_id='other'), replace(config, instance_id='other'),
                    replace(config, hub_url='https://other.example')]:
        client = ObservationClient()
        with pytest.raises(ValueError):
            HubGateway(altered, client=client, session_factory=no_session)
        assert not client.calls
    # Same identity can restart; no prior execution receipts are replayed.
    with HubGateway(config, client=ObservationClient(), session_factory=no_session) as gateway:
        gateway.run_once()


def test_legacy_execution_state_is_not_silently_adopted_by_observer(tmp_path):
    config = options(tmp_path)
    store = GatewayStore(config.state_dir)
    store.set_metadata('instance_id', 'alpha')
    store.accept_turn(turn_id='unknown', local_session_id='local', prompt_digest='digest')
    store.close()
    client = ObservationClient()
    with pytest.raises(ValueError, match='state'):
        HubGateway(config, client=client, session_factory=no_session)
    assert not client.calls
    store = GatewayStore(config.state_dir)
    try:
        assert store.turn('unknown')['state'] == 'accepted'
    finally:
        store.close()


def test_cli_observe_defaults_are_zero_and_explicit_conflicts_are_rejected(tmp_path, monkeypatch):
    config = options(tmp_path)
    seen = []

    class FakeGateway:
        instance_id, generation = 'alpha', 1
        def __init__(self, opts, **kwargs):
            seen.append(normalize_config(opts))
        def run_forever(self):
            pass
        def close(self):
            pass

    monkeypatch.setattr(hub_cli, 'HubGateway', FakeGateway)
    monkeypatch.setattr(hub_cli.HubClient, 'from_env', lambda *a, **kw: ObservationClient())
    args = ['agent', '--observe-only', '--hub', config.hub_url, '--cwd', config.cwd,
            '--state-dir', config.state_dir, '--instance-id', 'alpha', '--project-id', 'abr']
    assert hub_cli.hub_main(args) == 0
    assert seen[0].sessions == seen[0].max_sessions == 0 and seen[0].observe_only
    assert hub_cli.hub_main(args + ['--sessions', '1']) != 0


@pytest.mark.parametrize('kind', ['lock-link', 'lock-hardlink', 'database-link', 'public-directory'])
def test_unsafe_state_refused_without_database_access(tmp_path, monkeypatch, kind):
    import os
    import pbatch.hub_gateway_store as storage
    config = options(tmp_path)
    state = Path(config.state_dir)
    state.mkdir(mode=0o700)
    target = tmp_path / 'foreign'
    target.write_text('must not change')
    target.chmod(0o600)
    if kind == 'lock-link':
        (state / 'hub-gateway.lock').symlink_to(target)
    elif kind == 'lock-hardlink':
        os.link(target, state / 'hub-gateway.lock')
    elif kind == 'database-link':
        (state / 'hub-gateway.sqlite3').symlink_to(target)
    else:
        state.chmod(0o755)
    monkeypatch.setattr(storage.sqlite3, 'connect', lambda *a, **k: pytest.fail('unsafe SQLite opened'))
    client = ObservationClient()
    with pytest.raises((ValueError, OSError)):
        HubGateway(config, client=client, session_factory=no_session)
    assert not client.calls and target.read_text() == 'must not change'
    if kind == 'public-directory':
        assert state.stat().st_mode & 0o777 == 0o755


def test_second_writer_rejected_before_sqlite_is_opened(tmp_path, monkeypatch):
    import pbatch.hub_gateway as module
    config = options(tmp_path)
    with HubGateway(config, client=ObservationClient(), session_factory=no_session):
        monkeypatch.setattr(module, 'GatewayStore', lambda *a: pytest.fail('second writer opened SQLite'))
        with pytest.raises(RuntimeError, match='another Gateway'):
            HubGateway(config, client=ObservationClient(), session_factory=no_session)
