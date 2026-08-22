"""TUI Runtime 的版本化事件、能力与 Turn 状态协议。

本模块只定义纯数据与确定性校验，不执行 IO，也不依赖具体 TUI 或 Runtime。
未知事件类型 fail closed；未知顶层字段则原位保留，以支持兼容的 schema 演进。
`runtime.unknown_item` 是 adapter 遇到未知 provider item 时的显式占位事件，
用于 fail open 地保留有界证据而不让事件流崩溃。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Collection, Literal


RUNTIME_EVENT_SCHEMA_VERSION = 1
RUNTIME_PROTOCOL_VERSION = 1
MAX_EVENT_PAYLOAD_BYTES = 64 * 1024
MAX_EVENT_EXTENSIONS_BYTES = 16 * 1024
MAX_EVENT_LINE_BYTES = 128 * 1024

RuntimeEventKind = Literal[
    "session.created",
    "session.resumed",
    "session.forked",
    "session.archived",
    "session.disconnected",
    "turn.queued",
    "turn.started",
    "turn.steered",
    "turn.interrupting",
    "turn.completed",
    "turn.failed",
    "turn.interrupted",
    "message.started",
    "message.delta",
    "message.completed",
    "plan.updated",
    "command.started",
    "command.output",
    "command.completed",
    "tool.started",
    "tool.completed",
    "tool.failed",
    "file.changed",
    "workspace.snapshot",
    "validation.started",
    "validation.completed",
    "gate.verdict",
    "approval.requested",
    "approval.resolved",
    "approval.expired",
    "actor.spawned",
    "actor.status",
    "actor.completed",
    "review.finding",
    "artifact.created",
    "usage.updated",
    "runtime.warning",
    "runtime.error",
    "runtime.unknown_item",
    "runtime.capabilities_changed",
]

RUNTIME_EVENT_KINDS = frozenset({
    "session.created", "session.resumed", "session.forked",
    "session.archived", "session.disconnected", "turn.queued",
    "turn.started", "turn.steered", "turn.interrupting", "turn.completed",
    "turn.failed", "turn.interrupted", "message.started", "message.delta",
    "message.completed", "plan.updated", "command.started", "command.output",
    "command.completed", "tool.started", "tool.completed", "tool.failed",
    "file.changed", "workspace.snapshot", "validation.started",
    "validation.completed", "gate.verdict", "approval.requested",
    "approval.resolved", "approval.expired", "actor.spawned", "actor.status",
    "actor.completed", "review.finding", "artifact.created", "usage.updated",
    "runtime.warning", "runtime.error", "runtime.unknown_item",
    "runtime.capabilities_changed",
})

_EVENT_ID_RE = re.compile(r"^evt_[A-Za-z0-9_-]+$")
_EVENT_FIELDS = frozenset({
    "schema_version", "event_id", "seq", "occurred_at", "workspace_id",
    "session_id", "run_id", "turn_id", "actor_id", "kind", "payload",
    "extensions",
})
_REQUIRED_EVENT_FIELDS = _EVENT_FIELDS - {
    "schema_version", "payload", "extensions",
}


def _json_bytes(value: Any, label: str) -> bytes:
    """把协议值编码为严格 JSON；不可序列化值统一转为 ValueError。"""
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
        return text.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError(f"{label} must be valid JSON") from exc


def _bounded_mapping(value: Any, label: str, maximum: int) -> dict[str, Any]:
    """校验映射类型和 JSON 字节上限，并复制一层隔离调用方。"""
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a dict")
    copied = dict(value)
    size = len(_json_bytes(copied, label))
    if size > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes ({size} bytes)")
    return copied


def _reject_json_constant(value: str) -> None:
    """拒绝 JSON 标准之外的 NaN 与 Infinity 常量。"""
    raise ValueError(f"invalid JSON constant: {value}")


@dataclass
class RuntimeEvent:
    """Runtime 与 TUI 间的可信事件 Envelope。"""

    event_id: str
    seq: int
    occurred_at: str
    workspace_id: str
    session_id: str
    run_id: str
    turn_id: str
    actor_id: str
    kind: RuntimeEventKind
    payload: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)
    schema_version: int = RUNTIME_EVENT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self._validate_scalars()
        self.payload = _bounded_mapping(
            self.payload, "payload", MAX_EVENT_PAYLOAD_BYTES)
        self.extensions = _bounded_mapping(
            self.extensions, "extensions", MAX_EVENT_EXTENSIONS_BYTES)
        self._unknown_fields: dict[str, Any] = {}

    def _validate_scalars(self) -> None:
        """校验版本、标识、序号和事件类型。"""
        if (isinstance(self.schema_version, bool)
                or not isinstance(self.schema_version, int)
                or self.schema_version != RUNTIME_EVENT_SCHEMA_VERSION):
            raise ValueError(f"unsupported schema_version: {self.schema_version!r}")
        if not isinstance(self.event_id, str) or not _EVENT_ID_RE.fullmatch(self.event_id):
            raise ValueError(f"invalid event_id: {self.event_id!r}")
        if isinstance(self.seq, bool) or not isinstance(self.seq, int) or self.seq < 0:
            raise ValueError("seq must be a non-negative integer")
        if not isinstance(self.kind, str) or self.kind not in RUNTIME_EVENT_KINDS:
            raise ValueError(f"unknown runtime event kind: {self.kind!r}")

    @property
    def unknown_fields(self) -> dict[str, Any]:
        """返回未知顶层字段副本；这些字段不会混入 extensions。"""
        return dict(self._unknown_fields)

    def _as_mapping(self) -> dict[str, Any]:
        """生成核心字段覆盖未知字段的可序列化映射。"""
        value = dict(self._unknown_fields)
        value.update({
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "seq": self.seq,
            "occurred_at": self.occurred_at,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "actor_id": self.actor_id,
            "kind": self.kind,
            "payload": self.payload,
            "extensions": self.extensions,
        })
        return value

    def to_jsonl(self) -> str:
        """序列化为带换行结尾的一条有界 JSONL 记录。"""
        self._validate_scalars()
        self.payload = _bounded_mapping(
            self.payload, "payload", MAX_EVENT_PAYLOAD_BYTES)
        self.extensions = _bounded_mapping(
            self.extensions, "extensions", MAX_EVENT_EXTENSIONS_BYTES)
        encoded = _json_bytes(self._as_mapping(), "runtime event") + b"\n"
        if len(encoded) > MAX_EVENT_LINE_BYTES:
            raise ValueError(
                f"runtime event line exceeds {MAX_EVENT_LINE_BYTES} bytes")
        return encoded.decode("utf-8")

    @classmethod
    def from_jsonl(cls, line: str) -> "RuntimeEvent":
        """解析单条 JSONL；未知顶层字段保持原层级并在输出时回放。"""
        text = _validate_jsonl_line(line)
        try:
            value = json.loads(text, parse_constant=_reject_json_constant)
        except (TypeError, ValueError) as exc:
            raise ValueError("runtime event line is not valid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("runtime event line must contain a JSON object")
        missing = sorted(_REQUIRED_EVENT_FIELDS - set(value))
        if missing:
            raise ValueError(f"runtime event missing fields: {', '.join(missing)}")
        known = {name: value[name] for name in _EVENT_FIELDS if name in value}
        try:
            event = cls(**known)
        except TypeError as exc:
            raise ValueError("runtime event fields are invalid") from exc
        event._unknown_fields = {
            name: item for name, item in value.items() if name not in _EVENT_FIELDS
        }
        return event


def _validate_jsonl_line(line: str) -> str:
    """在 JSON 解析前执行类型、字节和单物理行边界检查。"""
    if not isinstance(line, str):
        raise ValueError("runtime event line must be text")
    try:
        size = len(line.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise ValueError("runtime event line is not valid UTF-8 text") from exc
    if size > MAX_EVENT_LINE_BYTES:
        raise ValueError(f"runtime event line exceeds {MAX_EVENT_LINE_BYTES} bytes")
    text = line[:-1] if line.endswith("\n") else line
    if text.endswith("\r"):
        text = text[:-1]
    if not text or "\n" in text or "\r" in text:
        raise ValueError("runtime event JSONL must contain exactly one line")
    return text


RICH_CAPABILITY_NAMES = (
    "structured_events", "session_resume", "session_fork", "turn_steer",
    "message_queue", "typed_approvals", "review", "subagents", "images",
    "usage", "instruction_sources",
)
LEGACY_CAPABILITY_NAMES = ("message_stream", "interrupt")


def _capability_defaults(runtime: str) -> dict[str, bool]:
    """生成 runtime 对应的能力基线；只有 Legacy 默认开放最小能力。"""
    value = {
        name: False for name in RICH_CAPABILITY_NAMES + LEGACY_CAPABILITY_NAMES
    }
    if runtime == "legacy":
        value.update({name: True for name in LEGACY_CAPABILITY_NAMES})
    return value


@dataclass
class RuntimeCapabilities:
    """一次 Runtime 连接真实支持的版本化能力集合。"""

    protocol_version: int = RUNTIME_PROTOCOL_VERSION
    runtime: str = "legacy"
    capabilities: dict[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (isinstance(self.protocol_version, bool)
                or not isinstance(self.protocol_version, int)
                or self.protocol_version != RUNTIME_PROTOCOL_VERSION):
            raise ValueError(
                f"unsupported protocol_version: {self.protocol_version!r}")
        if not isinstance(self.runtime, str) or not self.runtime:
            raise ValueError("runtime must be a non-empty string")
        if not isinstance(self.capabilities, dict):
            raise ValueError("capabilities must be a dict")
        merged = _capability_defaults(self.runtime)
        for name, enabled in self.capabilities.items():
            if not isinstance(name, str) or not isinstance(enabled, bool):
                raise ValueError("capability names must be strings with bool values")
            if name in merged:
                merged[name] = enabled
        if self.runtime == "legacy" and not all(
                merged[name] for name in LEGACY_CAPABILITY_NAMES):
            raise ValueError("legacy runtime requires message_stream and interrupt")
        self.capabilities = merged

    def supports(self, capability: str) -> bool:
        """查询能力；未知能力按不支持处理。"""
        return self.capabilities.get(capability, False) is True

    def __getattr__(self, name: str) -> bool:
        """允许以属性形式读取已声明能力，同时保持 wire schema 为能力字典。"""
        if name in RICH_CAPABILITY_NAMES or name in LEGACY_CAPABILITY_NAMES:
            return self.supports(name)
        raise AttributeError(name)

    def to_dict(self) -> dict[str, Any]:
        """返回与 capability handshake 相同形状的独立字典。"""
        return {
            "protocol_version": self.protocol_version,
            "runtime": self.runtime,
            "capabilities": dict(self.capabilities),
        }


class TurnState:
    """Turn 状态常量；状态迁移必须通过 :func:`turn_transition`。"""

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTING = "INTERRUPTING"
    INTERRUPTED = "INTERRUPTED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    DENIED = "DENIED"
    DISCONNECTED = "DISCONNECTED"
    RECONCILING = "RECONCILING"
    UNKNOWN = "UNKNOWN"


TURN_STATES = frozenset({
    TurnState.QUEUED, TurnState.RUNNING, TurnState.SUCCEEDED, TurnState.FAILED,
    TurnState.INTERRUPTING, TurnState.INTERRUPTED, TurnState.WAITING_APPROVAL,
    TurnState.DENIED, TurnState.DISCONNECTED, TurnState.RECONCILING,
    TurnState.UNKNOWN,
})
TERMINAL_TURN_STATES = frozenset({
    TurnState.SUCCEEDED, TurnState.FAILED, TurnState.INTERRUPTED,
    TurnState.UNKNOWN,
})
TURN_TRANSITIONS = {
    TurnState.QUEUED: frozenset({TurnState.RUNNING}),
    TurnState.RUNNING: frozenset({
        TurnState.SUCCEEDED, TurnState.FAILED, TurnState.INTERRUPTING,
        TurnState.WAITING_APPROVAL, TurnState.DISCONNECTED, TurnState.UNKNOWN,
    }),
    TurnState.INTERRUPTING: frozenset({
        TurnState.INTERRUPTED, TurnState.FAILED,
    }),
    TurnState.WAITING_APPROVAL: frozenset({
        TurnState.RUNNING, TurnState.DENIED, TurnState.INTERRUPTING,
        TurnState.DISCONNECTED, TurnState.UNKNOWN,
    }),
    TurnState.DENIED: frozenset({TurnState.FAILED, TurnState.INTERRUPTED}),
    TurnState.DISCONNECTED: frozenset({TurnState.RECONCILING}),
    TurnState.RECONCILING: frozenset({
        TurnState.RUNNING, TurnState.UNKNOWN, TurnState.SUCCEEDED,
        TurnState.FAILED,
    }),
    TurnState.SUCCEEDED: frozenset(),
    TurnState.FAILED: frozenset(),
    TurnState.INTERRUPTED: frozenset(),
    TurnState.UNKNOWN: frozenset(),
}


def is_terminal_turn_state(state: str) -> bool:
    """判断 Turn 是否处于不可再迁移的终态。"""
    return state in TERMINAL_TURN_STATES


def can_turn_transition(current: str, next_state: str) -> bool:
    """判断迁移是否合法；未知状态返回 False。"""
    return next_state in TURN_TRANSITIONS.get(current, frozenset())


def turn_transition(current: str, next_state: str) -> str:
    """执行纯状态迁移；未知、终态后或表外迁移均抛 ValueError。"""
    if current not in TURN_STATES:
        raise ValueError(f"unknown current turn state: {current!r}")
    if next_state not in TURN_STATES:
        raise ValueError(f"unknown next turn state: {next_state!r}")
    if not can_turn_transition(current, next_state):
        allowed = sorted(TURN_TRANSITIONS[current]) or "terminal"
        raise ValueError(
            f"illegal turn transition {current} -> {next_state} (allowed: {allowed})")
    return next_state


def is_duplicate(event: RuntimeEvent, event_ids: Collection[str]) -> bool:
    """按 event_id 判断事件是否已经投递，不修改调用方集合。"""
    return event.event_id in event_ids


@dataclass
class EventSequenceTracker:
    """按 event_id 去重，并校验每个 run 的新事件 seq 严格递增。"""

    event_ids: set[str] = field(default_factory=set)
    last_seq_by_run: dict[str, int] = field(default_factory=dict)

    def is_duplicate(self, event: RuntimeEvent) -> bool:
        """查询事件是否已接收。"""
        return is_duplicate(event, self.event_ids)

    def accept(self, event: RuntimeEvent) -> bool:
        """接收新事件返回 True；重复返回 False；乱序抛 ValueError。"""
        if self.is_duplicate(event):
            return False
        previous = self.last_seq_by_run.get(event.run_id)
        if previous is not None and event.seq <= previous:
            raise ValueError(
                f"non-monotonic seq for run {event.run_id!r}: "
                f"{event.seq} <= {previous}")
        self.event_ids.add(event.event_id)
        self.last_seq_by_run[event.run_id] = event.seq
        return True
