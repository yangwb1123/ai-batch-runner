"""Bounded, item-aware projection for one turn's assistant messages."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any


MESSAGE_KINDS = frozenset({
    "message.started", "message.delta", "message.completed",
})
ITEM_ID_BYTES = 512


@dataclass(frozen=True)
class MessageView:
    """Immutable aggregate consumed by presentation and result projection."""

    text: str = ""
    start: int = 0
    generation: int = 0


@dataclass
class _MessageItem:
    text: str = ""
    omitted: int = 0


class MessageProjection:
    """Preserve message items while sharing one bounded UTF-8 text budget."""

    def __init__(self, max_bytes: int, max_items: int = 256,
                 separator: str = "\n\n"):
        _positive_int(max_bytes, "max_bytes")
        _positive_int(max_items, "max_items")
        self.max_bytes = max_bytes
        self.max_items = max_items
        self.separator = str(separator)
        self.reset()

    def reset(self) -> None:
        self._items: dict[str, _MessageItem] = {}
        self._order: list[str] = []
        self._implicit_serial = 0
        self._implicit_active = ""
        self._view = MessageView()

    def snapshot(self) -> MessageView:
        return self._view

    def validate(self, kind: str, item_id: Any = "") -> bool:
        """Preview item-capacity acceptance without changing message state."""
        if kind not in MESSAGE_KINDS:
            raise ValueError(f"unsupported message event: {kind}")
        key, _activate = self._resolve_key(kind, item_id)
        if key not in self._items and len(self._order) >= self.max_items:
            raise OverflowError("message item projection is full")
        return True

    def consume(self, kind: str, item_id: Any = "",
                text: Any = "") -> MessageView:
        if kind not in MESSAGE_KINDS:
            raise ValueError(f"unsupported message event: {kind}")
        key, activate_implicit = self._resolve_key(kind, item_id)
        self._ensure_item(key)
        if activate_implicit:
            self._implicit_serial += 1
            self._implicit_active = key
        if kind == "message.started":
            return self._view
        value = str(text or "")
        if kind == "message.completed":
            self._complete(key, value)
        else:
            self._append(key, value)
        return self._view

    def _resolve_key(self, kind: str, item_id: Any) -> tuple[str, bool]:
        raw = str(item_id or "")
        if raw:
            return "explicit:" + _bounded_key(raw), False
        if kind == "message.started" or not self._implicit_active:
            return f"implicit:{self._implicit_serial + 1}", True
        return self._implicit_active, False

    def _ensure_item(self, key: str) -> None:
        if key in self._items:
            return
        if len(self._order) >= self.max_items:
            raise OverflowError("message item projection is full")
        self._items[key] = _MessageItem()
        self._order.append(key)

    def _append(self, key: str, value: str) -> None:
        item = self._items[key]
        boundary = self._append_boundary(key, item, value)
        expected, dropped = _tail_window(
            self._view.text + boundary + value, self.max_bytes)
        expected_start = self._view.start + dropped
        item.text += value
        self._compact()
        text, start = self._render()
        generation = self._view.generation
        if text != expected or start != expected_start:
            generation += 1
        self._view = MessageView(text, start, generation)

    def _append_boundary(self, key: str, item: _MessageItem,
                         value: str) -> str:
        if not value or item.text or item.omitted:
            return ""
        index = self._order.index(key)
        prior = (self._items[other].text for other in self._order[:index])
        return self.separator if any(prior) else ""

    def _complete(self, key: str, value: str) -> None:
        text, dropped = _tail_window(value, self.max_bytes)
        self._items[key] = _MessageItem(text, dropped)
        self._compact()
        aggregate, start = self._render()
        self._view = MessageView(
            aggregate, start, self._view.generation + 1)

    def _compact(self) -> None:
        excess = sum(_byte_len(item.text) for item in self._items.values())
        excess -= self.max_bytes
        for key in self._order:
            if excess <= 0:
                break
            item = self._items[key]
            before = _byte_len(item.text)
            if not before:
                continue
            suffix, dropped = _drop_prefix_bytes(item.text, min(excess, before))
            removed = before - _byte_len(suffix)
            item.text = suffix
            item.omitted += dropped
            excess -= removed

    def _render(self) -> tuple[str, int]:
        joined = self.separator.join(
            self._items[key].text for key in self._order
            if self._items[key].text)
        text, dropped = _tail_window(joined, self.max_bytes)
        omitted = sum(item.omitted for item in self._items.values())
        return text, omitted + dropped


def _bounded_key(value: str) -> str:
    encoded = value.encode("utf-8", "replace")
    if len(encoded) <= ITEM_ID_BYTES:
        return value
    digest = hashlib.sha256(encoded).hexdigest()[:32]
    prefix = encoded[:ITEM_ID_BYTES - len(digest) - 1].decode(
        "utf-8", "ignore")
    return f"{prefix}#{digest}"


def _drop_prefix_bytes(value: str, count: int) -> tuple[str, int]:
    encoded = value.encode("utf-8", "replace")
    keep = max(0, len(encoded) - max(0, count))
    suffix = encoded[-keep:].decode("utf-8", "ignore") if keep else ""
    return suffix, len(value) - len(suffix)


def _tail_window(value: Any, maximum: int) -> tuple[str, int]:
    text = str(value or "")
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= maximum:
        return text, 0
    suffix = encoded[-maximum:].decode("utf-8", "ignore")
    return suffix, len(text) - len(suffix)


def _byte_len(value: str) -> int:
    return len(value.encode("utf-8", "replace"))


def _positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
