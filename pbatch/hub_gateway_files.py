"""Explicit workspace inspection capture and terminal-acknowledged delivery."""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from .hub_client import HubError
from .hub_file_capture import MANIFEST_ENV, root_identity, unavailable
from .hub_file_snapshot_models import (
    MAX_WIRE_BYTES, SCHEMA, VERIFICATION, file_metadata, validate_upload,
)
from .hub_gateway_files_store import GatewayFileQueue
from .hub_gateway_delivery import DeliveryResult
from .hub_gateway_protocol import is_fencing_error
from .launcher import bundle_root, is_frozen, script_argv
from .process_exec import run_bounded

log = logging.getLogger("pi-batch.hub-gateway")
CAPTURE_SECONDS = 5.0


def _capture_script():
    if is_frozen():
        return bundle_root() / 'scripts' / 'capture-web-files.py'
    source = Path(__file__).resolve().parent.parent / 'scripts' / 'capture-web-files.py'
    if source.is_file():
        return source
    from .assets import _distribution_asset_root
    root = _distribution_asset_root()
    if root is None:
        raise ValueError('owned capture script unavailable')
    return root / 'scripts' / 'capture-web-files.py'


def bounded_capture(gateway, identity, cancel_event=None):
    paths = list(gateway.options.share_files)
    try:
        environment = dict(os.environ)
        environment['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent)
        environment[MANIFEST_ENV] = json.dumps({
            'workspace': gateway.options.cwd, 'paths': paths, 'identity': identity})
        result = run_bounded(script_argv(str(_capture_script())), gateway.options.state_dir,
                             CAPTURE_SECONDS, MAX_WIRE_BYTES, False, 'file inspection',
                             cancel_event=cancel_event or gateway._stop, env=environment)
        if not result.ok:
            return unavailable(paths, 'read_timeout' if result.timed_out else 'capture_unavailable')
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or set(value) != {'files', 'unavailable'}:
            raise ValueError('invalid capture response')
        return value
    except Exception:
        return unavailable(paths, 'capture_unavailable')


class GatewayFiles:
    def __init__(self, gateway):
        self.gateway = gateway
        self.paths = list(gateway.options.share_files)
        self.queue = GatewayFileQueue(gateway._store)
        self.identity = root_identity(gateway.options.cwd) if self.paths else None

    def _body(self, turn_id, session_id):
        gateway = self.gateway
        return {'schema': SCHEMA, 'instance_id': gateway.instance_id,
                'generation': gateway.generation, 'session_id': session_id,
                'turn_id': turn_id, 'snapshot_set_id': uuid.uuid4().hex,
                **unavailable(self.paths, 'turn_not_completed')}

    def synthetic(self, turn_id, session_id, event_id):
        if not self.paths:
            return
        try:
            body = self._body(turn_id, session_id)
            if self.queue.reserve(body, event_id):
                self.queue.save(body)
        except Exception:
            log.warning('File inspection unavailable (persistence_failed)')

    def capture(self, managed, turn_id, completed):
        if not self.paths:
            return
        try:
            self._capture(managed, turn_id, completed)
        except Exception:
            # This evidence failure cannot rewrite the already completed task.
            log.warning('File inspection unavailable (capture_or_persistence_failed)')

    def _capture(self, managed, turn_id, completed):
        gateway = self.gateway
        with gateway._session_lock:
            if (gateway._stop.is_set() or managed.active_turn != turn_id
                    or not managed.terminal_event_id):
                return
            body = self._body(turn_id, managed.hub_id)
            if not self.queue.reserve(body, managed.terminal_event_id):
                log.warning('File inspection unavailable (existing_receipt_or_capacity)')
                return
            cancelled = managed.cancel_requested
        result = unavailable(self.paths, 'cancel_requested' if cancelled else 'turn_not_completed')
        if completed and not cancelled:
            result = bounded_capture(gateway, self.identity, _CaptureStop(gateway, managed, turn_id))
        with gateway._session_lock:
            if gateway._stop.is_set() or managed.active_turn != turn_id:
                return
            if managed.cancel_requested:
                result = unavailable(self.paths, 'cancel_requested')
            self.queue.save(_captured_body(body, result, self.paths))

    def flush(self) -> DeliveryResult:
        gateway = self.gateway
        with gateway._session_lock:
            if gateway._stop.is_set() or not self.paths:
                return DeliveryResult()
            rows = self.queue.pending(gateway.generation)
        for row in rows:
            try:
                response = gateway.client.publish_file_snapshots(row['body'])
            except HubError as error:
                self._failure(row, error)
                return DeliveryResult(blocked=True)
            with gateway._session_lock:
                if gateway._stop.is_set() or row['generation'] != gateway.generation:
                    return DeliveryResult(blocked=True)
                if _valid_ack(row['body'], response):
                    self.queue.finish(row['turn_id'], 'acknowledged')
                else:
                    self.queue.failure(row['turn_id'], 'invalid_acknowledgement')
                    return DeliveryResult(blocked=True)
        return DeliveryResult(advanced=len(rows))

    def _failure(self, row, error):
        gateway = self.gateway
        with gateway._session_lock:
            if gateway._stop.is_set() or row['generation'] != gateway.generation:
                return
            if is_fencing_error(error):
                self.queue.finish(row['turn_id'], 'obsolete', 'generation_rejected')
            elif ((400 <= error.status < 500 and error.status not in {408, 429})
                  or (error.status == 507 and error.code == 'snapshot_capacity')):
                self.queue.finish(row['turn_id'], 'rejected', 'upload_rejected')
            else:
                self.queue.failure(row['turn_id'], 'upload_unknown')
        if is_fencing_error(error):
            raise error


def _valid_ack(body, response):
    if not isinstance(response, dict):
        return False
    fields = ('schema', 'instance_id', 'session_id', 'turn_id', 'snapshot_set_id')
    return (all(response.get(key) == body[key] for key in fields)
            and response.get('verification') == VERIFICATION
            and response.get('files') == [file_metadata(row) for row in body['files']]
            and response.get('unavailable') == body['unavailable'])


def _captured_body(body, result, paths):
    try:
        candidate = validate_upload(dict(body, **result))
        actual = [row['path'] for row in candidate['files'] + candidate['unavailable']]
        if set(actual) == set(paths):
            return candidate
    except (TypeError, ValueError, KeyError):
        pass
    return dict(body, **unavailable(paths, 'capture_unavailable'))


class _CaptureStop:
    """One owned turn's cancellation combined with the Gateway stop event."""

    def __init__(self, gateway, managed, turn_id):
        self.gateway, self.managed, self.turn_id = gateway, managed, turn_id

    def is_set(self):
        return (self.gateway._stop.is_set() or self.managed.cancel_requested
                or self.managed.active_turn != self.turn_id)
