"""Independent, bounded lifecycle delivery to explicitly configured ecosystem sinks."""

from __future__ import annotations

import logging
import re
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .hub_ecosystem_http import IntegrationError
from .hub_integration_store import IntegrationStore
from .hub_store_helpers import HubStoreError, identifier


def configured_sinks(settings):
    if not isinstance(settings, list) or len(settings) > 64:
        raise ValueError("integrations must be a list of at most 64 sinks")
    result, seen = [], set()
    for entry in settings:
        if not isinstance(entry, dict):
            raise ValueError("each integration must be an object")
        config = dict(entry)
        kind, cursor = config.pop("kind", ""), config.pop("start_cursor", None)
        if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int)
                                   or not 0 <= cursor < 2**63):
            raise ValueError("start_cursor must be a nonnegative 64-bit integer")
        if kind == "audit":
            from .hub_audit import AuditSink
            sink = AuditSink(config)
        elif kind == "im":
            from .hub_messaging import MessagingSink
            sink = MessagingSink(config)
        else:
            raise ValueError("integration kind must be audit or im")
        identifier(sink.tenant_id, "tenant_id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", sink.name):
            raise ValueError("integration name must be a stable identifier")
        key = (sink.tenant_id, sink.name)
        if key in seen:
            raise ValueError("duplicate integration name within tenant")
        seen.add(key)
        result.append((sink, cursor))
    return result


class HubIntegrations:
    def __init__(self, hub, settings=None, *, configured=None):
        pairs = configured if configured is not None else configured_sinks(
            [] if settings is None else settings)
        self.store = IntegrationStore(hub)
        self.sinks = {(sink.tenant_id, sink.name): sink for sink, _ in pairs}
        for sink, cursor in pairs:
            self.store.bind(sink, cursor)
        self._order = deque(self.sinks)
        self._stop = threading.Event()
        self._thread = None
        self._pool = None
        self._futures = {}

    def start(self):
        if self._thread is not None or not self.sinks:
            return
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hub-delivery")
        self._thread = threading.Thread(target=self._run, daemon=True, name="hub-integration-dispatch")
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._pool is not None:
            self._pool.shutdown(wait=True, cancel_futures=True)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.tick(background=True)
            except Exception:
                logging.error("Hub integration coordinator unavailable")
            self._stop.wait(0.5)

    def tick(self, *, background=False):
        self.store.recover_stalled()
        for key, future in list(self._futures.items()):
            if future.done():
                del self._futures[key]
                try:
                    future.result()
                except Exception:
                    logging.error("Hub integration persistence unavailable")
        for _ in range(len(self._order)):
            key = self._order[0]
            self._order.rotate(-1)
            if background and len(self._futures) >= 4:
                break
            sink = self.sinks[key]
            self.store.project(sink)
            if key in self._futures:
                continue
            due = self.store.due(sink, limit=1)
            if not due:
                continue
            if background:
                self._futures[key] = self._pool.submit(self._deliver, sink, due[0])
            else:
                self._deliver(sink, due[0])

    def _deliver(self, sink, pending):
        row = self.store.claim(sink.tenant_id, pending["delivery_id"])
        if row is None:
            return
        receipt, error = None, None
        try:
            if sink.fingerprint != row["fingerprint"]:
                raise IntegrationError("target_changed", retryable=False)
            receipt = sink.deliver(row["payload"], row["delivery_id"])
            self.store._payload(receipt)
        except IntegrationError as exc:
            error = exc
        except Exception:
            error = IntegrationError("delivery_internal_error", retryable=False)
        self.store.finish(row, receipt=receipt if error is None else None, error=error)

    def retry(self, tenant, key):
        row = self.store.get(tenant, key, private=True)
        sink = self.sinks.get((tenant, row["sink_name"]))
        if sink is None:
            raise HubStoreError("sink_unconfigured", "delivery sink is no longer configured")
        try:
            return self.store.retry(tenant, key, sink)
        except IntegrationError as exc:
            raise HubStoreError(exc.code, "delivery cannot be prepared for retry") from None
