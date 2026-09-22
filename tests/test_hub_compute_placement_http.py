"""Placement endpoint framing, verbs, identity and bounded transport contracts."""

from email.message import Message
import http.client
import json
from urllib.parse import urlsplit

import pytest

from pbatch.hub_compute_client import ComputeClient
from pbatch.hub_compute_placement import validate_read_headers
from pbatch.hub_service import PREFIX
from test_hub_compute import Fabric
from test_hub_compute_transport import transport  # noqa: F401


@pytest.fixture
def endpoint(transport):
    fabric = Fabric()
    connection = transport.compute.connection("team")
    connection.client = lambda: fabric
    task = ComputeClient(transport.user).submit(transport.session, {"argv": ["true"]}, "diagnose")
    return transport, task, fabric


def call(endpoint, *, method="GET", query="", token="user", headers=(), body=None, task_id=None):
    transport, task, _ = endpoint
    url = urlsplit(transport.url)
    client = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
    try:
        target = task["task_id"] if task_id is None else task_id
        client.putrequest(method, PREFIX + "/tasks/" + target + "/placement" + query)
        client.putheader("Authorization", "Bearer " + token)
        for name, value in headers:
            client.putheader(name, value)
        if body is not None:
            client.putheader("Content-Length", str(len(body)))
            client.putheader("Content-Type", "application/json")
        client.endheaders(body)
        response = client.getresponse()
        raw = response.read()
        data = json.loads(raw) if raw else {}
        return response.status, data, dict(response.headers)
    finally:
        client.close()


def test_http_exact_dto_has_no_command_workdir_or_control_side_effects(endpoint):
    transport, task, fabric = endpoint
    event_history = transport.hub.events("team", transport.session)
    status, payload, headers = call(endpoint)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert set(payload) == {"data"}
    assert payload["data"]["task_id"] == task["task_id"]
    assert payload["data"]["devices"] == [{"device_id": "device-b", "instance_id": "remote",
                                          "eligible": True, "reasons": []}]
    assert len(json.dumps(payload, ensure_ascii=False).encode()) <= 65536
    assert not fabric.requests and ComputeClient(transport.user).get(task["task_id"]) == task
    assert transport.hub.events("team", transport.session) == event_history


def test_malformed_fabric_mapping_returns_structured_503_over_http(endpoint):
    _, _, fabric = endpoint
    original = fabric.inventory

    def broken(ids):
        result = original(ids)
        result["devices"][0]["dynamic_state"] = None
        return result

    fabric.inventory = broken
    status, payload, _ = call(endpoint)
    assert status == 503 and payload["error"]["code"] == "placement_unavailable"
    assert not fabric.requests


@pytest.mark.parametrize("token,status", [("reader", 403), ("gateway", 403), ("outsider", 404),
                                         ("invalid", 401)])
def test_http_identity_scope_and_tenant_boundaries(endpoint, token, status):
    assert call(endpoint, token=token)[0] == status


@pytest.mark.parametrize("query", ["?limit=0", "?limit=21", "?limit=1&limit=2",
                                   "?after=a&after=b", "?project_id=private", "?reserve=true",
                                   "?after=bad%00id", "?after=bad%7Fid", "?after=%20bad"])
def test_http_rejects_unknown_duplicate_or_invalid_query(endpoint, query):
    status, payload, _ = call(endpoint, query=query)
    assert status == 400 and payload["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("cursor", ["%FF", "%ED%A0%80"])
def test_percent_encoded_invalid_utf8_cursor_is_not_silently_replaced(endpoint, cursor):
    status, payload, _ = call(endpoint, query="?after=" + cursor)
    assert status == 400 and payload["error"]["code"] == "invalid_request"


def test_valid_utf8_replacement_character_remains_a_valid_cursor(endpoint):
    status, payload, _ = call(endpoint, query="?after=%EF%BF%BD")
    assert status == 200 and payload["data"]["devices"] == []


@pytest.mark.parametrize("task_id", ["%FF", "%ED%A0%80"])
def test_percent_encoded_invalid_utf8_task_id_is_rejected(endpoint, task_id):
    status, payload, _ = call(endpoint, task_id=task_id)
    assert status == 400 and payload["error"]["code"] == "invalid_request"


def test_valid_utf8_replacement_character_is_looked_up_as_task_id(endpoint):
    status, payload, _ = call(endpoint, task_id="%EF%BF%BD")
    assert status == 404 and payload["error"]["code"] == "not_found"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_http_wrong_methods_are_405_without_mutation(endpoint, method):
    status, payload, _ = call(endpoint, method=method, body=b"{}" if method == "POST" else None)
    assert status == 405
    if method != "HEAD":
        assert payload["error"]["code"] == "method_not_allowed"
    assert not endpoint[2].requests


@pytest.mark.parametrize("headers", [[("Content-Length", "1")],
                                      [("Content-Length", "0"), ("Content-Length", "0")],
                                      [("Transfer-Encoding", "chunked")], [("Transfer-Encoding", "")]])
def test_http_framing_guard_rejects_without_reading_a_body(endpoint, headers):
    assert call(endpoint, headers=headers)[0] == 400


def test_get_body_is_rejected_and_explicit_zero_length_is_accepted(endpoint):
    assert call(endpoint, body=b"{}")[0] == 400
    assert call(endpoint, headers=[("Content-Length", "0")])[0] == 200


def test_options_remains_existing_cors_preflight(endpoint):
    status, _, headers = call(endpoint, method="OPTIONS")
    assert status == 204 and "GET" in headers["Access-Control-Allow-Methods"]


def test_header_guard_is_narrow_and_decodes_placement_route_only():
    headers = Message()
    headers["Content-Length"] = "10"
    validate_read_headers(PREFIX + "/tasks/work", headers)
    validate_read_headers("/outside/tasks/work/placement", headers)
    with pytest.raises(ValueError):
        validate_read_headers(PREFIX + "/tasks/work/%70lacement", headers)
