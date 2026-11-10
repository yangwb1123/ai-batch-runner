"""Shared offline validation for Hub startup and --check-config.

No listener, database, background workers, issuer requests or credential exchange.
Optional adapters may read only their explicitly configured local credential files.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit

from .config_document import load_yaml_document

_AUTH_FIELDS = {'mode', 'issuer', 'jwks_url', 'audience', 'algorithms', 'ca_file',
                'grants', 'web_read_grants'}
_GRANT_FIELDS = {'tenant_id', 'subject', 'client_id', 'kind', 'instances', 'scopes',
                 'compute_instances', 'token_sha256'}


def _read_config(filename):
    path = Path(filename).expanduser()
    if path.is_symlink():
        raise ValueError('Hub config must not be a symlink')
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 262144:
            raise ValueError('Hub config must be a nonempty regular file within 256 KiB')
        raw = stream.read(262145)
        after = os.fstat(stream.fileno())
        if (len(raw) != before.st_size or after.st_size != before.st_size
                or (after.st_mtime_ns, after.st_ctime_ns) != (before.st_mtime_ns, before.st_ctime_ns)
                or not os.path.samestat(after, path.lstat())):
            raise ValueError('Hub config changed during read')
    return raw


def read_settings(filename: str) -> dict:
    import yaml
    raw = _read_config(filename)
    try:
        value = load_yaml_document(raw.decode('utf-8'), yaml, unique_keys=True)
    except (yaml.YAMLError, ValueError, UnicodeError, RecursionError):
        raise ValueError('Hub YAML is malformed, ambiguous or exceeds document limits') from None
    if not isinstance(value, dict) or not isinstance(value.get('auth'), dict):
        raise ValueError('Hub config requires auth settings')
    if set(value) - {'auth', 'allowed_origins', 'compute', 'integrations', 'identity', 'web'}:
        raise ValueError('unknown Hub config setting')
    _auth_fields(value['auth'])
    _origins(value.get('allowed_origins', []))
    return value


def _auth_fields(auth):
    if set(auth) - _AUTH_FIELDS:
        raise ValueError('unknown Hub authentication setting')
    grants = auth.get('grants', [])
    if not isinstance(grants, list) or len(grants) > 1000:
        raise ValueError('Hub grants must be a bounded list')
    for grant in grants:
        if not isinstance(grant, dict) or set(grant) - _GRANT_FIELDS:
            raise ValueError('unknown Hub grant setting')


def _origins(origins):
    if not isinstance(origins, list) or any(not isinstance(item, str) for item in origins):
        raise ValueError('allowed_origins must be a list of exact origins')
    for origin in origins:
        parsed = urlsplit(origin)
        if (parsed.scheme not in {'https', 'http'} or not parsed.hostname
                or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise ValueError('allowed_origins must contain exact HTTP(S) origins')


@dataclass
class PreparedHub:
    web: object
    identity: object
    compute: dict
    sinks: list


@contextmanager
def prepared_components(settings, auth):
    from .hub_compute_config import connections
    from .hub_identity import IdentityGate
    from .hub_web import HubWeb
    web = None
    try:
        if 'web' in settings:
            web = HubWeb(settings, auth)
            from .hub_web_assets import bundle
            bundle()  # Broken compiled resources must fail before opening SQLite/listening.
        identity = IdentityGate(settings.get('identity', []))
        compute = connections(settings.get('compute', []))
        sinks = _prepared_sinks(settings.get('integrations', []))
        yield PreparedHub(web, identity, compute, sinks)
    finally:
        if web is not None:
            web.close()


def _prepared_sinks(settings):
    from .hub_integrations import configured_sinks
    from .hub_ecosystem_http import private_token, IntegrationError
    sinks = configured_sinks(settings)
    for sink, _ in sinks:
        try:
            private_token(sink.transport.token_file)
        except IntegrationError:
            raise ValueError('integration service credential is unavailable or not private') from None
    return sinks


def check_report(prepared, auth):
    """Counts describe validated local declarations, never real identity acceptance."""
    return {
        'schema': 'abr.hub.config-check/v1', 'result': 'passed',
        'web_enabled': prepared.web is not None,
        'web_control_enabled': bool(prepared.web and prepared.web.control),
        'declared_grants': len(auth.grants),
        'declared_tenant_read_policies': len(auth.web_read_grants),
        'identity_gate_enabled': prepared.identity.enabled,
        'configured_compute_tenants': len(prepared.compute),
        'configured_sinks': len(prepared.sinks),
        'external_acceptance': {'result': 'not_executed', 'reason':
            'Offline check only; no real login, grant ownership, Gateway, TLS edge or recovery verified.'},
        'side_effects': {'listener_started': False, 'database_opened': False,
                         'network_requests': 0, 'grants_changed': False},
    }
