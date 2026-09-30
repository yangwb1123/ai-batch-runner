"""In-memory wire tests for the narrow facade, not an SDK compliance claim."""

import io
import json
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call as mock_call, patch

from pbatch import llm_pool_mcp as mcp


def request(method, params=None, request_id=2):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def initialize(version="2025-11-25"):
    return request("initialize", {"protocolVersion": version, "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "1"}}, 1)


def notification(method="notifications/initialized", **fields):
    return {"jsonrpc": "2.0", "method": method, **fields}


def wire(*messages):
    return b"".join((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
                    for message in messages)


def call(name="pool_status", arguments=None, **fields):
    params = {"name": name, **fields}
    if arguments is not None:
        params["arguments"] = arguments
    return request("tools/call", params)


def cached_usage(state="fresh"):
    windows = {"5h": {"status": "rate-limited", "percent": 100, "resets_at": 600},
               "weekly": {"status": "ok", "percent": 25, "resets_at": 700},
               "monthly": {"status": "ok", "percent": 10, "resets_at": 800}}
    return {"state": state, "checked_at": 100, "fresh_until": 200, "next_refresh_at": 300,
            "error": "unavailable" if state == "error" else None,
            "windows": {} if state == "unknown" else windows}


class BoundedInput(io.BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.read_sizes = []

    def readline(self, size=-1):
        if not 0 < size <= mcp.MAX_LINE_BYTES + 1:
            raise AssertionError("unbounded input read")
        self.read_sizes.append(size)
        return super().readline(size)


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            models={"model-a": "messages", "模型-b": "chat/completions"},
            accounts=(SimpleNamespace(id="a", credential_file="/private/key-file"),),
            token_file="/private/token-file", secret="sk-test-never-expose")
        self.store = Mock(spec=["status", "events"])
        self.store.status.return_value = {"state": "available",
                                          "accounts": [{"id": "a", "quota_group": "g"}]}
        self.store.events.return_value = [{"kind": "success", "model": "模型-b"}]

    def exchange(self, *messages, handshake=True, raw=None):
        prefix = wire(initialize(), notification()) if handshake else b""
        source = BoundedInput(prefix + (wire(*messages) if raw is None else raw))
        output = io.BytesIO()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = mcp.serve_mcp(self.settings, self.store, source, output)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertFalse(source.closed)
        self.assertFalse(output.closed)
        data = output.getvalue()
        replies = [json.loads(line) for line in data.splitlines()]
        self.assertTrue(not data or data.endswith(b"\n"))
        return code, replies[1:] if handshake else replies, source, data

    def assert_error(self, reply, code):
        self.assertEqual(reply["jsonrpc"], "2.0")
        self.assertEqual(reply["error"]["code"], code)
        self.assertNotIn("result", reply)

    def test_initialize_negotiates_supported_version_and_only_tools(self):
        for version in ("2025-11-25", "2025-06-18", "2099-01-01"):
            with self.subTest(version=version):
                _, replies, _, _ = self.exchange(initialize(version), handshake=False)
                self.assertEqual(replies[0]["id"], 1)
                result = replies[0]["result"]
                self.assertEqual(result["protocolVersion"], "2025-11-25")
                self.assertEqual(result["capabilities"], {"tools": {"listChanged": False}})
                self.assertTrue(result["serverInfo"]["name"])
                self.assertTrue(result["serverInfo"]["version"])
        self.assertEqual(self.store.mock_calls, [])

    def test_client_optional_features_and_metadata_are_not_reflected(self):
        init = initialize()
        init["params"]["capabilities"] = {"roots": {"listChanged": True}, "sampling": {},
                                          "elicitation": {}, "tasks": {}, "experimental": {}}
        init["params"]["clientInfo"].update(title="client title", websiteUrl="https://example.org")
        init["params"]["_meta"] = {"private": self.settings.secret}
        _, replies, _, data = self.exchange(init, notification(), request("tools/list"),
                                             handshake=False)
        self.assertEqual(len(replies), 2)
        self.assertNotIn(self.settings.secret.encode(), data)
        self.assertEqual(replies[0]["result"]["capabilities"], {"tools": {"listChanged": False}})

    def test_tools_wait_for_both_handshake_messages_and_allow_ping(self):
        messages = [notification(), request("tools/list"), call(), request("ping"),
                    initialize(), request("tools/list"), call(), request("ping"),
                    notification(), request("tools/list"), call()]
        _, replies, _, _ = self.exchange(*messages, handshake=False)
        for index in (0, 1, 4, 5):
            self.assert_error(replies[index], -32002)
        for index in (2, 6):
            self.assertEqual(replies[index]["result"], {})
        self.assertEqual(len(replies[-2]["result"]["tools"]), 3)
        self.store.status.assert_called_once_with(self.settings.accounts, model="", require_usage=False)

    def test_bad_initialized_notifications_do_not_open_gate(self):
        for bad in (notification(params=[]), notification(params={"unexpected": True}),
                    notification(jsonrpc="1.0"), notification(params={"_meta": None})):
            with self.subTest(bad=bad):
                _, replies, _, _ = self.exchange(initialize(), bad, call(), notification(),
                                                 request("ping"), handshake=False)
                self.assertEqual(len(replies), 3)
                self.assert_error(replies[1], -32002)
                self.assertEqual(replies[2]["result"], {})
        self.assertEqual(self.store.mock_calls, [])

    def test_initialized_request_cannot_open_gate_and_reinitialize_is_rejected(self):
        _, replies, _, _ = self.exchange(initialize(), request("notifications/initialized"),
                                         call(), notification(), initialize(), call(),
                                         handshake=False)
        self.assert_error(replies[1], -32601)
        self.assert_error(replies[2], -32002)
        self.assert_error(replies[3], -32600)
        self.assertFalse(replies[4]["result"]["isError"])

    def test_initialize_validation_can_be_retried(self):
        bad_params = [{}, None, [], {"protocolVersion": "2025-11-25"}]
        for key, value in (("protocolVersion", 2025), ("protocolVersion", ""),
                           ("capabilities", []), ("clientInfo", {}), ("clientInfo", None),
                           ("clientInfo", {"name": True, "version": "1"}), ("extra", 1)):
            params = initialize()["params"]
            params[key] = value
            bad_params.append(params)
        for params in bad_params:
            with self.subTest(params=params):
                bad = request("initialize")
                bad["params"] = params
                _, replies, _, _ = self.exchange(bad, notification(), call(), initialize(),
                                                 notification(), call(), handshake=False)
                self.assert_error(replies[0], -32602)
                self.assert_error(replies[1], -32002)
                self.assertIn("protocolVersion", replies[2]["result"])
                self.assertFalse(replies[3]["result"]["isError"])

    def test_tool_schemas_annotations_and_config_names_only(self):
        _, replies, _, data = self.exchange(request("tools/list"))
        tools = {tool["name"]: tool for tool in replies[0]["result"]["tools"]}
        self.assertEqual(set(tools), {"pool_status", "pool_events", "pool_usage"})
        for tool in tools.values():
            self.assertEqual(tool["inputSchema"]["type"], "object")
            self.assertFalse(tool["inputSchema"]["additionalProperties"])
            self.assertTrue(tool["annotations"]["readOnlyHint"])
            self.assertFalse(tool["annotations"]["destructiveHint"])
            self.assertTrue(tool["annotations"]["idempotentHint"])
            self.assertFalse(tool["annotations"]["openWorldHint"])
        model = tools["pool_status"]["inputSchema"]["properties"]["model"]
        self.assertEqual(model, {"type": "string", "enum": sorted(self.settings.models)})
        limit = tools["pool_events"]["inputSchema"]["properties"]["limit"]
        self.assertEqual((limit["type"], limit["minimum"], limit["maximum"]), ("integer", 1, 100))
        account = tools["pool_usage"]["inputSchema"]["properties"]["account"]
        self.assertEqual(account, {"type": "string", "enum": ["a"]})
        description = tools["pool_usage"]["description"]
        for phrase in ("cached", "freshness", "advisory", "No live refresh", "secret/key"):
            self.assertIn(phrase, description)
        for forbidden in (b"/private/", b"sk-test-", b"credential_file", b"chat/completions"):
            self.assertNotIn(forbidden, data)
        self.assertEqual(self.store.mock_calls, [])

    def test_status_and_events_return_json_text_and_use_exact_arguments(self):
        _, replies, _, _ = self.exchange(call(), call(arguments={"model": "模型-b"}),
                                         call("pool_events"), call("pool_events", {"limit": 1}),
                                         call("pool_events", {"limit": 100}))
        self.assertEqual(self.store.status.call_args_list,
                         [mock_call(self.settings.accounts, model="", require_usage=False),
                          mock_call(self.settings.accounts, model="模型-b", require_usage=False)])
        self.assertEqual(self.store.events.call_args_list,
                         [mock_call(mcp.DEFAULT_EVENT_LIMIT), mock_call(1), mock_call(100)])
        for index, reply in enumerate(replies):
            result = reply["result"]
            self.assertFalse(result["isError"])
            self.assertEqual(len(result["content"]), 1)
            self.assertEqual(result["content"][0]["type"], "text")
            expected = self.store.status.return_value if index < 2 else self.store.events.return_value
            self.assertEqual(json.loads(result["content"][0]["text"]), expected)

    def test_tool_argument_fields_and_types_fail_closed(self):
        invalid = [call(arguments={"model": value}) for value in (None, 1, True, [], {}, "", "other")]
        invalid += [call("pool_events", {"limit": value})
                    for value in (None, True, False, 0, -1, 101, 1.0, "1", [], {})]
        invalid += [call(arguments={"account_id": "a"}), call("pool_events", {"path": "/private"}),
                    call("pool_events", {"limit": 1, "reset": True})]
        for message in invalid:
            with self.subTest(message=message):
                _, replies, _, _ = self.exchange(message)
                self.assert_error(replies[0], -32602)
        self.assertEqual(self.store.mock_calls, [])

    def test_invalid_call_envelopes_and_unnegotiated_tasks(self):
        params_cases = [None, [], {}, {"name": 1}, {"name": {}}, {"name": "pool_status", "x": 1},
                        {"name": "pool_status", "task": {"ttl": 10}},
                        {"name": "pool_status", "_meta": []}]
        params_cases += [{"name": "pool_status", "arguments": value}
                        for value in (None, [], "", 0, False)]
        for params in params_cases:
            with self.subTest(params=params):
                message = request("tools/call")
                message["params"] = params
                _, replies, _, _ = self.exchange(message)
                self.assert_error(replies[0], -32602)
        self.assertEqual(self.store.mock_calls, [])

    def test_ping_list_params_and_optional_request_metadata(self):
        for method in ("ping", "tools/list"):
            for params in ([], {"cursor": "made-up"}, {"unknown": True}, {"_meta": None}):
                with self.subTest(method=method, params=params):
                    _, replies, _, _ = self.exchange(request(method, params))
                    self.assert_error(replies[0], -32602)
        _, replies, _, _ = self.exchange(call(_meta={"progressToken": 5}))
        self.assertFalse(replies[0]["result"]["isError"])

    def test_arbitrary_admin_or_secret_methods_never_dispatch(self):
        names = ("set_enabled", "reset_auth", "acquire", "finish", "read_secret",
                 "pool_pause", "pool_resume", "__dict__", "status", "events")
        for name in names:
            with self.subTest(name=name):
                _, replies, _, _ = self.exchange(request(name), call(name))
                self.assert_error(replies[0], -32601)
                self.assert_error(replies[1], -32602)
        self.assertEqual(self.store.mock_calls, [])

    def test_notifications_never_respond_or_invoke_tools(self):
        messages = [notification("initialize", params=initialize()["params"]),
                    notification("tools/call", params={"name": "pool_status"}),
                    notification("reset_auth"), notification("ping"),
                    notification("tools/list", params=[]), request("ping")]
        _, replies, _, _ = self.exchange(*messages)
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 2, "result": {}}])
        self.assertEqual(self.store.mock_calls, [])

    def test_notification_initialize_does_not_start_handshake(self):
        init = initialize()
        del init["id"]
        _, replies, _, _ = self.exchange(init, notification(), call(), handshake=False)
        self.assertEqual(len(replies), 1)
        self.assert_error(replies[0], -32002)

    def test_execution_and_serialization_errors_redact_secrets_and_continue(self):
        secret = "sk-test-super-secret /private/credential_file api_key=private-key"
        for tool, method in (("pool_status", self.store.status), ("pool_events", self.store.events),
                             ("pool_usage", self.store.status)):
            for exception in (RuntimeError(secret), ValueError(secret), OSError(secret)):
                with self.subTest(tool=tool, exception=type(exception)):
                    method.side_effect = exception
                    _, replies, _, data = self.exchange(call(tool), request("ping"))
                    self.assertEqual(replies[0]["result"], {
                        "content": [{"type": "text", "text": '{"error":"Pool read failed"}'}],
                        "isError": True})
                    self.assertEqual(replies[1]["result"], {})
                    for fragment in (b"sk-test", b"/private", b"api_key", b"credential_file"):
                        self.assertNotIn(fragment, data)
            method.side_effect = None
        for value in ({"bad": object()}, {"bad": float("nan")}, {"bad": float("inf")}, []):
            self.store.status.return_value = value
            _, replies, _, _ = self.exchange(call())
            self.assertTrue(replies[0]["result"]["isError"])

    def test_event_store_contract_violations_are_tool_errors(self):
        for value in ({"secret": "not-a-list"}, [1, 2]):
            self.store.events.return_value = value
            _, replies, _, _ = self.exchange(call("pool_events", {"limit": 1}))
            self.assertTrue(replies[0]["result"]["isError"])

    def test_current_pool_config_interface_and_no_secret_file_access(self):
        from pbatch.llm_pool_config import PoolConfig
        from pbatch.llm_pool_store import PoolAccount

        self.settings = PoolConfig("test", (PoolAccount("a", "/private/key-file", "g"),),
                                   {"model-a": "messages"}, Path("/private/token-file"),
                                   Path("/private/state-file"))
        with patch("builtins.open", side_effect=AssertionError("must not open files")), \
                patch.object(Path, "open", side_effect=AssertionError("must not open paths")):
            _, replies, _, data = self.exchange(request("tools/list"), call())
        self.assertFalse(replies[1]["result"]["isError"])
        self.store.status.assert_called_once_with(self.settings.accounts, model="", require_usage=False)
        self.assertNotIn(b"/private/", data)

    def test_internal_errors_are_redacted_and_do_not_end_session(self):
        with patch.object(mcp, "_tools", side_effect=RuntimeError("/private/sk-secret")):
            _, replies, _, data = self.exchange(request("tools/list"), request("ping"))
        self.assertEqual(replies[0]["error"], {"code": -32603, "message": "Internal error"})
        self.assertEqual(replies[1]["result"], {})
        self.assertNotIn(b"private", data)

    def test_empty_model_configuration_still_allows_unfiltered_status(self):
        self.settings.models = {}
        _, replies, _, _ = self.exchange(request("tools/list"), call(), call(arguments={"model": ""}))
        schema = replies[0]["result"]["tools"][0]["inputSchema"]["properties"]["model"]
        self.assertEqual(schema, {"not": {}})
        self.assertFalse(replies[1]["result"]["isError"])
        self.assert_error(replies[2], -32602)

    def test_unicode_and_embedded_text_newlines_stay_in_one_output_frame(self):
        self.store.status.return_value = {"state": "模型\nsecond line\r\nthird"}
        code, replies, _, data = self.exchange(call())
        self.assertEqual(code, 0)
        self.assertEqual(len(data.splitlines()), 2)
        self.assertEqual(json.loads(replies[0]["result"]["content"][0]["text"]),
                         self.store.status.return_value)

    def test_malformed_json_utf8_duplicates_constants_and_depth_recover(self):
        bad_lines = [b"{", b"\xff", b"", b'{"x":NaN}', b'{"x":Infinity}', b'{"x":-Infinity}',
                     b'{"x":1e9999}', b'{"x":1,"x":2}',
                     b'{"jsonrpc":"2.0","id":2,"method":"ping"} {}']
        for line in bad_lines:
            with self.subTest(line=line[:50]):
                code, replies, _, _ = self.exchange(raw=line + b"\n" + wire(request("ping")))
                self.assertEqual(code, 0)
                self.assert_error(replies[0], -32700)
                self.assertNotIn("id", replies[0])
                self.assertEqual(replies[1]["result"], {})

    def test_deeply_nested_frames_are_bounded_and_recover(self):
        """Nesting is bounded without misreporting a parse error.

        A nested array is *valid* JSON, so when the decoder accepts it the
        honest answer is Invalid Request, not Parse error. Only nesting deep
        enough to exhaust the decoder's own recursion guard is a parse failure.
        Both must be bounded, and the next request must still be served.
        """
        for depth, expected in ((2000, -32600), (200000, -32700)):
            with self.subTest(depth=depth):
                line = b"[" * depth + b"]" * depth
                code, replies, _, _ = self.exchange(
                    raw=line + b"\n" + wire(request("ping")))
                self.assertEqual(code, 0)
                self.assert_error(replies[0], expected)
                self.assertNotIn("id", replies[0])
                self.assertEqual(replies[1]["result"], {})

    def test_invalid_envelopes_and_batches_never_execute(self):
        invalid = [None, [], [call()], True, "text", {}, {"jsonrpc": "2.0", "id": 2},
                   {**request("ping"), "jsonrpc": "1.0"},
                   {**request("ping"), "method": []}, {**request("ping"), "extra": True}]
        invalid += [request("ping", request_id=value) for value in (None, True, False, [], {})]
        for message in invalid:
            with self.subTest(message=message):
                _, replies, _, _ = self.exchange(message)
                self.assert_error(replies[0], -32600)
        self.assertEqual(self.store.mock_calls, [])

    def test_request_ids_are_preserved_in_success_and_error(self):
        for request_id in (0, -1, 2.5, "", "编号"):
            with self.subTest(request_id=request_id):
                _, replies, _, _ = self.exchange(request("ping", request_id=request_id),
                                                 request("unknown", request_id=request_id))
                self.assertEqual([reply["id"] for reply in replies], [request_id, request_id])

    def test_exact_frame_bound_accepts_valid_frame(self):
        line = wire(request("ping"))
        frame = line[:-1] + b" " * (mcp.MAX_LINE_BYTES - len(line)) + b"\n"
        code, replies, _, _ = self.exchange(raw=frame, handshake=False)
        self.assertEqual(code, 0)
        self.assertEqual(replies[0]["result"], {})

    def test_oversized_frame_stops_without_drain_or_later_execution(self):
        for frame in (b"x" * (mcp.MAX_LINE_BYTES * 2), b" " * mcp.MAX_LINE_BYTES + b"\n"):
            with self.subTest(size=len(frame)):
                raw = frame + wire(initialize(), notification(), call())
                code, replies, source, _ = self.exchange(raw=raw, handshake=False)
                self.assertEqual(code, 1)
                self.assertEqual(source.read_sizes, [mcp.MAX_LINE_BYTES + 1])
                self.assertEqual(source.tell(), mcp.MAX_LINE_BYTES + 1)
                self.assert_error(replies[0], -32700)
        self.assertEqual(self.store.mock_calls, [])

    def test_eof_and_unterminated_frames(self):
        code, replies, _, _ = self.exchange(raw=b"", handshake=False)
        self.assertEqual((code, replies), (0, []))
        for raw in (wire(call())[:-1], b"{", b" " * mcp.MAX_LINE_BYTES):
            code, replies, _, _ = self.exchange(raw=raw, handshake=False)
            self.assertEqual(code, 1)
            self.assert_error(replies[0], -32700)
        self.assertEqual(self.store.mock_calls, [])

    def test_default_binary_streams_flush_and_transport_failure(self):
        output = io.BytesIO()
        source = BoundedInput(wire(request("ping")))
        with patch.object(mcp.sys, "stdin", SimpleNamespace(buffer=source)), \
                patch.object(mcp.sys, "stdout", SimpleNamespace(buffer=output)):
            self.assertEqual(mcp.serve_mcp(self.settings, self.store), 0)
        self.assertEqual(json.loads(output.getvalue())["result"], {})
        for operation in ("readline", "write", "flush"):
            source, output = io.BytesIO(wire(request("ping"))), io.BytesIO()
            target = source if operation == "readline" else output
            with patch.object(target, operation, side_effect=OSError("/private/sk-secret")):
                self.assertEqual(mcp.serve_mcp(self.settings, self.store, source, output), 1)

    def test_each_response_is_flushed_before_next_read(self):
        source = BoundedInput(wire(request("ping"), request("ping")))
        output = io.BytesIO()
        readline = source.readline

        def checked_readline(size):
            self.assertEqual(flush.call_count, len(source.read_sizes))
            return readline(size)

        with patch.object(output, "flush", wraps=output.flush) as flush, \
                patch.object(source, "readline", side_effect=checked_readline):
            self.assertEqual(mcp.serve_mcp(self.settings, self.store, source, output), 0)
        self.assertEqual(flush.call_count, 2)

    def test_report_byte_boundary_and_oversized_events_are_sanitized(self):
        overhead = len(json.dumps({"detail": ""}, separators=(",", ":")))
        self.store.status.return_value = {"detail": "x" * (mcp.MAX_REPORT_BYTES - overhead)}
        _, replies, _, data = self.exchange(call())
        self.assertFalse(replies[0]["result"]["isError"])
        self.assertTrue(all(len(line) + 1 <= mcp.MAX_LINE_BYTES for line in data.splitlines()))
        self.store.status.return_value["detail"] += "sk-secret"
        self.store.events.return_value = [{"detail": "\u6c49" * mcp.MAX_REPORT_BYTES}]
        _, replies, _, data = self.exchange(call(), call("pool_events"), request("ping"))
        for reply in replies[:2]:
            self.assertTrue(reply["result"]["isError"])
            self.assertEqual(json.loads(reply["result"]["content"][0]["text"]),
                             {"error": "Pool read failed"})
        self.assertNotIn(b"sk-secret", data)
        self.assertEqual(replies[2]["result"], {})

    def test_exact_output_frame_limit_and_one_byte_overflow(self):
        message = call()
        message["id"] = ""
        _, replies, _, _ = self.exchange(message)
        overhead = len(json.dumps(replies[0], separators=(",", ":")).encode("utf-8")) + 1
        for extra in (0, 1):
            with self.subTest(extra=extra):
                message["id"] = "x" * (mcp.MAX_LINE_BYTES - overhead + extra)
                self.assertLess(len(wire(message)), mcp.MAX_LINE_BYTES)
                code, replies, _, data = self.exchange(message)
                self.assertEqual(code, extra)
                if extra:
                    self.assertEqual(replies, [])  # No partial frame or omitted known ID.
                else:
                    self.assertEqual(replies[0]["id"], message["id"])
                    self.assertEqual(len(data.splitlines()[-1]) + 1, mcp.MAX_LINE_BYTES)
                self.assertTrue(all(len(line) + 1 <= mcp.MAX_LINE_BYTES
                                    for line in data.splitlines()))

    def test_escaped_id_expansion_closes_without_partial_output(self):
        for method in ("ping", "unknown"):
            message = request(method, request_id="\u6c49" * (mcp.MAX_LINE_BYTES // 3 - 100))
            self.assertLess(len(wire(message)), mcp.MAX_LINE_BYTES)
            code, replies, _, data = self.exchange(message, request("ping"))
            self.assertEqual(code, 1)
            self.assertEqual(replies, [])
            self.assertEqual(len(data.splitlines()), 1)  # Only the initialize response.

    def live_pool(self):
        from pbatch.llm_pool_config import PoolConfig
        from pbatch.llm_pool_store import PoolAccount, PoolStore

        directory = tempfile.TemporaryDirectory(prefix="pbatch-mcp-test-")
        self.addCleanup(directory.cleanup)
        self.settings = PoolConfig("test", (PoolAccount("a", "/unread/secret-key", "g"),),
                                   {"model-a": "messages"}, Path("/unread/secret-token"),
                                   Path(directory.name) / "pool.sqlite")
        self.store = PoolStore(self.settings.state_file)

    def test_live_store_handshake_status_events_and_external_completion(self):
        self.live_pool()
        lease = self.store.acquire(self.settings.accounts, "model-a", 60)
        events_before = self.store.events(100)
        code, replies, _, data = self.exchange(call(), initialize(), call(), notification(),
                                              call(arguments={"model": "model-a"}),
                                              call("pool_events", {"limit": 1}), handshake=False)
        self.assertEqual(code, 0)
        self.assert_error(replies[0], -32002)
        self.assertEqual(replies[1]["result"]["protocolVersion"], "2025-11-25")
        self.assert_error(replies[2], -32002)
        status = json.loads(replies[3]["result"]["content"][0]["text"])
        events = json.loads(replies[4]["result"]["content"][0]["text"])
        self.assertEqual(status["in_flight"], 1)
        self.assertEqual(status["accounts"][0]["id"], "a")
        self.assertEqual(events, events_before[:1])
        self.assertEqual(events[0]["kind"], "acquire")
        self.assertEqual(self.store.events(100), events_before)
        self.assertNotIn(b"/unread/", data)
        self.assertNotIn(b"credential_file", data)
        self.store.finish(lease)
        _, replies, _, _ = self.exchange(call())
        self.assertEqual(json.loads(replies[0]["result"]["content"][0]["text"])["in_flight"], 0)

    def test_live_store_historical_blocks_overflow_report_limit(self):
        self.live_pool()
        with sqlite3.connect(self.settings.state_file) as connection:
            connection.executemany("INSERT INTO blocks VALUES(?,?,?,?,?,?)", [
                ("g", f"retired-model-{index}", "5h", time.time() + 3600, 1, f"v{index}")
                for index in range(3000)])
        _, replies, _, data = self.exchange(call(), call(arguments={"model": "model-a"}))
        self.assertTrue(replies[0]["result"]["isError"])
        self.assertFalse(replies[1]["result"]["isError"])
        self.assertNotIn(b"retired-model", data)
        self.assertTrue(all(len(line) + 1 <= mcp.MAX_LINE_BYTES for line in data.splitlines()))

    def test_live_store_corruption_returns_generic_tool_errors(self):
        self.live_pool()
        self.settings.state_file.write_bytes(b"corrupt /private/sk-secret")
        _, replies, _, data = self.exchange(call(), call("pool_events"), call("pool_usage"),
                                           request("ping"))
        for reply in replies[:3]:
            self.assertTrue(reply["result"]["isError"])
            self.assertEqual(json.loads(reply["result"]["content"][0]["text"]),
                             {"error": "Pool read failed"})
        self.assertEqual(replies[3]["result"], {})
        self.assertNotIn(b"sk-secret", data)
        self.assertNotIn(str(self.settings.state_file).encode(), data)

    def test_usage_projects_only_configured_cached_accounts_and_optional_filter(self):
        self.settings.accounts += (SimpleNamespace(id="b", credential_file="/private/b"),)
        first = {"id": "a", "quota_group": "g", "usage": cached_usage()}
        second = {"id": "b", "quota_group": "g", "usage": cached_usage("unknown")}
        self.store.status.return_value = {
            "state": "unavailable", "private": "sk-secret", "accounts": [
                {**first, "credential_file": "/private/a", "state": "disabled", "in_flight": 2},
                {**second, "blocks": [], "extra": "x" * mcp.MAX_REPORT_BYTES},
                {"id": "removed", "quota_group": "old", "usage": {"secret": "sk-secret"}}]}
        _, replies, _, data = self.exchange(call("pool_usage"), call("pool_usage", {"account": "b"}))
        for reply, expected in zip(replies, ([first, second], [second])):
            self.assertFalse(reply["result"]["isError"])
            self.assertEqual(json.loads(reply["result"]["content"][0]["text"]),
                             {"source": "cache", "advisory": True, "accounts": expected})
        self.assertEqual(self.store.status.call_args_list,
                         [mock_call(self.settings.accounts, model="")] * 2)
        self.store.events.assert_not_called()
        for forbidden in (b"sk-secret", b"/private/", b"credential_file", b"removed"):
            self.assertNotIn(forbidden, data)

    def test_usage_strictly_rejects_unknown_accounts_types_and_extra_fields(self):
        messages = [call("pool_usage", {"account": value})
                    for value in ("missing", "", None, False, 1, [], {})]
        messages += [call("pool_usage", {field: value}) for field, value in (
            ("model", "model-a"), ("refresh", True), ("limit", 1), ("account_id", "a"))]
        messages += [call("pool_usage", {"account": "a", "credential_file": "/private/key"})]
        for value in (None, [], "a", 1, False):
            message = call("pool_usage")
            message["params"]["arguments"] = value
            messages.append(message)
        for message in messages:
            with self.subTest(message=message):
                _, replies, _, data = self.exchange(message)
                self.assert_error(replies[0], -32602)
                self.assertNotIn(b"/private", data)
        self.assertEqual(self.store.mock_calls, [])

    def test_usage_preserves_expired_unknown_and_refreshing_cache_without_refresh(self):
        for state in ("fresh", "stale", "unknown", "error", "refreshing"):
            with self.subTest(state=state):
                cached = cached_usage(state)
                self.store.status.return_value["accounts"][0]["usage"] = cached
                _, replies, _, _ = self.exchange(call("pool_usage"))
                result = replies[0]["result"]
                self.assertFalse(result["isError"])
                payload = json.loads(result["content"][0]["text"])
                self.assertEqual(payload["source"], "cache")
                self.assertTrue(payload["advisory"])
                self.assertEqual(payload["accounts"][0]["usage"], cached)
        self.assertEqual(self.store.status.call_count, 5)
        self.store.events.assert_not_called()

    def test_usage_missing_cache_reports_unknown_not_zero_quota(self):
        _, replies, _, _ = self.exchange(call("pool_usage", {"account": "a"}))
        usage = json.loads(replies[0]["result"]["content"][0]["text"])["accounts"][0]["usage"]
        self.assertEqual(usage, {"state": "unknown", "checked_at": None, "fresh_until": None,
                                 "next_refresh_at": None, "error": None, "windows": {}})

    def test_usage_does_not_call_network_or_read_keys(self):
        self.store.status.return_value["accounts"][0]["usage"] = cached_usage("stale")
        with patch("socket.socket", side_effect=AssertionError("no network")) as socket, \
                patch("socket.create_connection", side_effect=AssertionError("no network")) as connect, \
                patch("builtins.open", side_effect=AssertionError("no keys")) as opened, \
                patch.object(Path, "open", side_effect=AssertionError("no keys")) as paths:
            _, replies, _, _ = self.exchange(call("pool_usage", {"account": "a"}))
        self.assertFalse(replies[0]["result"]["isError"])
        for forbidden in (socket, connect, opened, paths):
            forbidden.assert_not_called()
        self.store.status.assert_called_once_with(self.settings.accounts, model="")
        self.store.events.assert_not_called()

    def test_usage_report_budget_counts_projected_payload_and_utf8_escaping(self):
        cached = cached_usage("error")
        cached["error"] = ""
        row = {"id": "a", "quota_group": "g", "usage": cached}
        self.store.status.return_value["accounts"] = [row]
        payload = {"source": "cache", "advisory": True, "accounts": [row]}
        overhead = len(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
        cached["error"] = "x" * (mcp.MAX_REPORT_BYTES - overhead)
        _, replies, _, _ = self.exchange(call("pool_usage"))
        self.assertFalse(replies[0]["result"]["isError"])
        for text in (cached["error"] + "x", "汉" * (mcp.MAX_REPORT_BYTES // 3)):
            with self.subTest(length=len(text)):
                cached["error"] = text
                code, replies, _, data = self.exchange(call("pool_usage"), request("ping"))
                self.assertEqual(code, 0)
                self.assertEqual(replies[0]["result"], {
                    "content": [{"type": "text", "text": '{"error":"Pool read failed"}'}],
                    "isError": True})
                self.assertEqual(replies[1]["result"], {})
                self.assertTrue(all(len(line) + 1 <= mcp.MAX_LINE_BYTES
                                    for line in data.splitlines()))

    def test_status_still_returns_complete_snapshot_with_usage(self):
        self.store.status.return_value["accounts"][0]["usage"] = cached_usage("fresh")
        _, replies, _, _ = self.exchange(call(arguments={"model": "model-a"}))
        self.assertEqual(json.loads(replies[0]["result"]["content"][0]["text"]),
                         self.store.status.return_value)
        self.store.status.assert_called_once_with(self.settings.accounts, model="model-a",
                                                  require_usage=False)

    def test_status_preflight_setting_is_forwarded_but_usage_reads_only_cache(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.settings.usage_enabled = enabled
                self.store.status.reset_mock()
                _, replies, _, _ = self.exchange(call(arguments={"model": "model-a"}),
                                                 call("pool_usage", {"account": "a"}))
                self.assertTrue(all(not reply["result"]["isError"] for reply in replies))
                self.assertEqual(self.store.status.call_args_list, [
                    mock_call(self.settings.accounts, model="model-a", require_usage=enabled),
                    mock_call(self.settings.accounts, model="")])

    def test_status_live_store_preflight_requires_fresh_usage_when_enabled(self):
        self.live_pool()
        _, replies, _, _ = self.exchange(call())
        status = json.loads(replies[0]["result"]["content"][0]["text"])
        self.assertEqual(status["accounts"][0]["state"], "available")
        self.settings = replace(self.settings, usage_enabled=True)
        _, replies, _, _ = self.exchange(call(), call("pool_usage"))
        status = json.loads(replies[0]["result"]["content"][0]["text"])
        usage = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual(status["accounts"][0]["state"], "usage_unknown")
        self.assertEqual(status["state"], "unavailable")
        self.assertEqual(usage["accounts"][0]["usage"]["state"], "unknown")
        self.assertTrue(usage["advisory"])
        self.assertEqual(self.store.events(100), [])

    def test_usage_requires_handshake_and_notifications_do_not_read_cache(self):
        no_reply = notification("tools/call", params={"name": "pool_usage"})
        _, replies, _, _ = self.exchange(call("pool_usage"), no_reply, initialize(),
                                         call("pool_usage"), notification(), no_reply,
                                         call("pool_usage"), handshake=False)
        self.assert_error(replies[0], -32002)
        self.assert_error(replies[2], -32002)
        self.assertFalse(replies[3]["result"]["isError"])
        self.store.status.assert_called_once_with(self.settings.accounts, model="")

    def test_usage_live_store_read_preserves_admission_state_and_event_history(self):
        self.live_pool()
        lease = self.store.acquire(self.settings.accounts, "model-a", 60)
        before = self.store.events(100)
        _, replies, _, data = self.exchange(call("pool_usage", {"account": "a"}))
        self.assertFalse(replies[0]["result"]["isError"])
        payload = json.loads(replies[0]["result"]["content"][0]["text"])
        self.assertEqual(set(payload), {"source", "advisory", "accounts"})
        self.assertEqual(set(payload["accounts"][0]), {"id", "quota_group", "usage"})
        self.assertEqual(payload["accounts"][0]["usage"]["state"], "unknown")
        self.assertEqual(payload["accounts"][0]["usage"]["windows"], {})
        self.assertEqual(self.store.status(self.settings.accounts)["in_flight"], 1)
        self.assertEqual(self.store.events(100), before)
        self.assertNotIn(b"/unread/", data)
        self.store.finish(lease)

    def test_usage_live_store_reads_fresh_then_expired_cache_without_refreshing(self):
        with patch("pbatch.llm_pool_store.time.time", return_value=1000) as clock:
            self.live_pool()
            ticket = self.store.begin_usage_refresh(self.settings.accounts[0], ttl=30)
            windows = {name: {"status": "ok", "percent": 25, "resets_at": 2000}
                       for name in ("5h", "weekly", "monthly")}
            self.store.finish_usage_refresh(ticket, windows, max_age=10)
            before = self.store.events(100)
            for now, state in ((1001, "fresh"), (1011, "stale")):
                with self.subTest(state=state):
                    clock.return_value = now
                    _, replies, _, _ = self.exchange(call("pool_usage", {"account": "a"}))
                    self.assertFalse(replies[0]["result"]["isError"])
                    payload = json.loads(replies[0]["result"]["content"][0]["text"])
                    usage = payload["accounts"][0]["usage"]
                    self.assertEqual(usage["state"], state)
                    self.assertEqual(usage["windows"], windows)
                    self.assertEqual(usage["checked_at"], 1000)
                    self.assertEqual(usage["fresh_until"], 1010)
                    self.assertEqual(self.store.events(100), before)

    def test_usage_malformed_store_cache_is_a_sanitized_tool_error(self):
        for rows in (None, {}, [None], [{"id": "a"}],
                     [{"id": "a", "quota_group": "g", "usage": None}]):
            with self.subTest(rows=rows):
                self.store.status.return_value = {"accounts": rows}
                _, replies, _, _ = self.exchange(call("pool_usage"))
                self.assertTrue(replies[0]["result"]["isError"])
                self.assertEqual(json.loads(replies[0]["result"]["content"][0]["text"]),
                                 {"error": "Pool read failed"})


if __name__ == "__main__":
    unittest.main()
