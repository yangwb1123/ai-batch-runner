"""Offline startup validation must not authenticate, listen, create state or leak YAML."""
import json
import os
import socket
import urllib.request

import pytest

from pbatch import hub_http
from pbatch.hub_store import HubStore


def settings():
    return {
        'auth': {'mode': 'jwt', 'issuer': 'https://identity.test',
                 'jwks_url': 'https://identity.test/keys', 'audience': 'agent-hub',
                 'grants': [{'tenant_id': 'team', 'subject': 'private-person', 'client_id': 'console',
                             'kind': 'user', 'instances': ['alpha'],
                             'scopes': ['agent.instances:read', 'agent.sessions:read']}]},
        'allowed_origins': ['https://abr.test'],
        'web': {'public_origin': 'https://abr.test', 'client_id': 'console', 'tenant_id': 'team'},
        'identity': [], 'integrations': [], 'compute': [],
    }


@pytest.fixture
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('offline config validation performed an external or persistent side effect')
    monkeypatch.setattr(HubStore, '__init__', forbidden)
    monkeypatch.setattr(hub_http.HubHTTPServer, '__init__', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', forbidden)
    return forbidden


def invoke(tmp_path, value):
    path = tmp_path / 'hub.yaml'
    path.write_text(json.dumps(value))
    return hub_http.hub_serve_main([
        '--check-config', '--json', '--config', str(path),
        '--database', str(tmp_path / 'not-created/hub.sqlite3')])


@pytest.mark.parametrize('control', [False, True])
def test_check_config_uses_real_validators_without_side_effects(tmp_path, capsys, offline, control):
    config = settings()
    if control:
        config['web']['control'] = True
    assert invoke(tmp_path, config) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report['result'] == 'passed' and report['web_enabled'] is True
    assert report['web_control_enabled'] is control
    assert report['external_acceptance']['result'] == 'not_executed'
    assert report['side_effects'] == {'listener_started': False, 'database_opened': False,
                                      'network_requests': 0, 'grants_changed': False}
    assert not (tmp_path / 'not-created').exists()
    for secret in ('private-person', 'console', 'identity.test', str(tmp_path)):
        assert secret not in output.out + output.err


@pytest.mark.parametrize('field,value', [('compute', {}), ('identity', {}), ('integrations', {}),
                                       ('web', {}), ('unknown', True)])
def test_invalid_components_fail_in_preflight(tmp_path, capsys, offline, field, value):
    config = settings()
    config[field] = value
    assert invoke(tmp_path, config) != 0
    assert json.loads(capsys.readouterr().out)['result'] == 'failed'
    assert not (tmp_path / 'not-created').exists()


def test_normal_startup_also_validates_components_before_state(tmp_path, offline):
    config = settings()
    config['compute'] = {}
    path = tmp_path / 'invalid-startup.json'
    path.write_text(json.dumps(config))
    with pytest.raises(SystemExit) as caught:
        hub_http.hub_serve_main(['--config', str(path), '--database', str(tmp_path / 'not-created/db')])
    assert caught.value.code == 2 and not (tmp_path / 'not-created').exists()


def test_optional_sink_checks_private_credentials_without_delivery(tmp_path, capsys, offline):
    config = settings()
    token_file = tmp_path / 'audit.token'
    config['integrations'] = [{'kind': 'audit', 'tenant_id': 'team', 'source_system': 'abr',
                               'base_url': 'https://audit.test', 'token_file': str(token_file)}]
    assert invoke(tmp_path, config) == 2
    token_file.write_text('PRIVATE-TEST-CREDENTIAL')
    token_file.chmod(0o600)
    assert invoke(tmp_path, config) == 0
    output = capsys.readouterr()
    assert 'PRIVATE-TEST-CREDENTIAL' not in output.out + output.err
    assert not (tmp_path / 'not-created').exists()


def test_assets_missing_fail_before_database_or_listener(tmp_path, capsys, offline, monkeypatch):
    from pbatch import hub_web_assets
    monkeypatch.setattr(hub_web_assets, 'ASSET_ROOT', tmp_path / 'missing-assets')
    assert invoke(tmp_path, settings()) != 0
    assert json.loads(capsys.readouterr().out)['result'] == 'failed'


def test_remote_plaintext_listener_rejected_even_in_check_mode(tmp_path, capsys, offline):
    path = tmp_path / 'hub.yaml'
    path.write_text(json.dumps(settings()))
    assert hub_http.hub_serve_main(['--config', str(path), '--host', '0.0.0.0',
                                  '--check-config', '--json']) != 0
    assert json.loads(capsys.readouterr().out)['result'] == 'failed'


@pytest.mark.parametrize('text', [
    'auth: {}\nauth: {}\n',
    'auth: {mode: jwt, mode: development}',
    'auth: {grants: [{kind: user, kind: gateway}]}',
    'auth: {grants: &loop [*loop]}',
    'auth: [private-secret: bad: syntax]',
    'auth: {mode: jwt, grants: [' + '[' * 70 + '0' + ']' * 70 + ']}',
    'base: &base {mode: jwt}\nauth: {<<: *base}',
])
def test_ambiguous_or_unbounded_yaml_is_rejected_without_body_disclosure(tmp_path, text):
    path = tmp_path / 'hub.yaml'
    path.write_text(text)
    with pytest.raises(ValueError) as caught:
        hub_http._settings(str(path))
    assert 'private-secret' not in str(caught.value)


@pytest.mark.parametrize('change', ['auth-field', 'grant-field', 'invalid-utf8', 'symlink', 'fifo'])
def test_unsafe_config_sources_and_unknown_fields_fail_closed(tmp_path, change):
    path = tmp_path / 'hub.yaml'
    config = settings()
    if change == 'auth-field':
        config['auth']['ignored_security_setting'] = True
    elif change == 'grant-field':
        config['auth']['grants'][0]['instance'] = 'typo'
    path.write_text(json.dumps(config))
    if change == 'invalid-utf8':
        path.write_bytes(b'\xff')
    elif change == 'symlink':
        original = tmp_path / 'original'
        path.rename(original)
        path.symlink_to(original)
    elif change == 'fifo':
        path.unlink()
        os.mkfifo(path)
    with pytest.raises((ValueError, OSError)):
        hub_http._settings(str(path))


def test_invalid_yaml_json_report_does_not_echo_secret_or_path(tmp_path, capsys, offline):
    path = tmp_path / 'private-profile.yaml'
    path.write_text('auth: [credential: NEVER-PRINT-THIS: bad]')
    assert hub_http.hub_serve_main(['--config', str(path), '--check-config', '--json']) == 2
    output = capsys.readouterr()
    assert json.loads(output.out)['result'] == 'failed'
    assert 'NEVER-PRINT-THIS' not in output.out + output.err
    assert str(path) not in output.out + output.err
