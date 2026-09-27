"""World state plane (AADM-G §2): durable snapshots + drift detection.

任务图表达"准备做什么"，世界状态表达"现在真实是什么"——两者必须分开。
快照：git HEAD + 工作区变更数 + 服务/协议版本 + 事实 + 单调版本号。
计划绑定一个世界版本；漂移超过阈值 → 不能机械执行旧计划，必须重新
验证前提（AADM-D §16 外部化状态的本地平面）。

状态存储采用追加式 WAL + 原子快照：WAL 写入并 fsync 后才更新快照，崩溃
恢复只接受连续、结构合法的记录；不完整的 WAL 尾记录视为崩溃残留。非 git
仓库仍 fail closed（head=unknown，漂移会要求重新验证）。

Usage:
    pi-batch world bind --out snapshot.json        # 绑定当前世界版本
    pi-batch world drift --bound snapshot.json     # 检查漂移
    pi-batch world drift --bound snapshot.json --threshold 3
    pi-batch world status --json                    # 查看持久化状态
    pi-batch world reconcile --json                 # 重新观测并持久化
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import config
from .text_io import read_text_bounded

try:
    from . import file_lock as fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

FABRIC_MODULES = ("pbatch/fabric.py", "pbatch/fabric_exec.py",
                  "pbatch/fabric_server.py", "pbatch/fabric_runner.py")
WORLD_SCHEMA_VERSION = 1
WORLD_LINE_MAX_BYTES = 256 * 1024
WORLD_FILE_MAX_BYTES = 16 * 1024 * 1024
WORLD_DIR_NAME = "world"


class WorldStoreError(RuntimeError):
    """世界状态损坏、越界或无法安全恢复。"""


def world_data_dir(cwd: str = "") -> Path:
    """返回遵循 PBATCH_DATA_DIR 的世界状态目录。"""
    return config.data_dir(cwd) / WORLD_DIR_NAME


def _empty_state() -> dict:
    return {"schema_version": WORLD_SCHEMA_VERSION, "event_sequence": 0,
            "world_sequence": 0, "snapshot": None, "plans": {}}


def _json_copy(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def _snapshot_identity(snapshot: dict) -> dict:
    """忽略观测时间和本地序号，得到可比较的世界内容。"""
    return {key: value for key, value in snapshot.items()
            if key not in ("as_of", "world_version")}


def _snapshot_digest(snapshot: dict) -> str:
    payload = json.dumps(_snapshot_identity(snapshot), sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_snapshot(snapshot: object) -> dict:
    if not isinstance(snapshot, dict):
        raise WorldStoreError("world snapshot must be an object")
    if not snapshot.get("version"):
        raise WorldStoreError("world snapshot requires a non-empty version")
    result = _json_copy(snapshot)
    result.setdefault("content_version", str(result["version"]))
    return result


def _validate_state_shape(value: object) -> dict:
    if not isinstance(value, dict):
        raise WorldStoreError("world state snapshot must be an object")
    if value.get("schema_version") != WORLD_SCHEMA_VERSION:
        raise WorldStoreError("unsupported world state schema version")
    for key in ("event_sequence", "world_sequence"):
        number = value.get(key)
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise WorldStoreError(f"invalid world state {key}")
    snapshot = value.get("snapshot")
    if snapshot is not None:
        snapshot = _validate_snapshot(snapshot)
        if snapshot.get("world_version") != value["world_sequence"]:
            raise WorldStoreError("world snapshot sequence does not match state")
    plans = value.get("plans", {})
    if not isinstance(plans, dict):
        raise WorldStoreError("world plan bindings must be an object")
    result = _empty_state()
    result.update(_json_copy(value))
    result["snapshot"] = snapshot
    result["plans"] = _json_copy(plans)
    if result["world_sequence"] > result["event_sequence"]:
        raise WorldStoreError("world sequence exceeds event sequence")
    return result


def _validate_event(record: object) -> dict:
    if not isinstance(record, dict):
        raise WorldStoreError("world WAL record must be an object")
    if record.get("schema_version") != WORLD_SCHEMA_VERSION:
        raise WorldStoreError("unsupported world WAL schema version")
    sequence = record.get("event_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0:
        raise WorldStoreError("world WAL event_sequence must be positive")
    if record.get("kind") not in ("world_snapshot", "plan_binding"):
        raise WorldStoreError("unknown world WAL record kind")
    return _json_copy(record)


def _apply_event(state: dict, record: dict) -> dict:
    """Apply one full-state event after its sequence has been checked."""
    kind = record["kind"]
    if kind == "world_snapshot":
        snapshot = _validate_snapshot(record.get("snapshot"))
        world_sequence = record.get("world_sequence")
        expected = state["world_sequence"] + 1
        if world_sequence != expected:
            raise WorldStoreError("world WAL world_sequence has a gap")
        snapshot["world_version"] = world_sequence
        state["snapshot"] = snapshot
        state["world_sequence"] = world_sequence
    else:
        plan_id = str(record.get("plan_id", "")).strip()
        binding = record.get("binding")
        if not plan_id or not isinstance(binding, dict):
            raise WorldStoreError("invalid world plan binding")
        current = state.get("snapshot") or {}
        if binding.get("plan_bound_world_version") != current.get("world_version"):
            raise WorldStoreError("plan binding does not match current world")
        state.setdefault("plans", {})[plan_id] = _json_copy(binding)
    state["event_sequence"] = record["event_sequence"]
    state["updated_at"] = record.get("at", "")
    return state


def _read_json(path: Path, maximum: int, label: str):
    try:
        return json.loads(read_text_bounded(path, maximum, label))
    except (OSError, ValueError) as exc:
        raise WorldStoreError(f"cannot read {label}: {exc}") from exc


def _fsync_directory(path: Path) -> None:
    try:
        flags = getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(str(path), os.O_RDONLY | flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise WorldStoreError(f"refusing symlink world state path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".",
                                              suffix=".tmp",
                                              dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise WorldStoreError(f"cannot write world snapshot {path}: {exc}") from exc
    finally:
        if descriptor != -1:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except OSError:
            pass


@contextlib.contextmanager
def _store_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise WorldStoreError(f"refusing symlink world lock: {path}")
    handle = path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield handle
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _append_wal(path: Path, record: dict) -> None:
    if path.is_symlink():
        raise WorldStoreError(f"refusing symlink world WAL: {path}")
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    if len(line.encode("utf-8")) > WORLD_LINE_MAX_BYTES:
        raise WorldStoreError("world WAL record exceeds line limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise WorldStoreError(f"cannot append world WAL {path}: {exc}") from exc


class WorldStore:
    """WAL + atomic snapshot store for one project's observed world."""

    def __init__(self, cwd: str = "", directory: Optional[Path] = None,
                 wal_path: Optional[Path] = None,
                 snapshot_path: Optional[Path] = None):
        base = Path(directory) if directory is not None else world_data_dir(cwd)
        if wal_path is not None:
            wal_path = Path(wal_path)
            base = base if directory is not None else wal_path.parent
        self.cwd = cwd or "."
        self.directory = base
        self.wal_path = Path(wal_path) if wal_path else base / "world.wal.jsonl"
        self.snapshot_path = (Path(snapshot_path) if snapshot_path
                              else base / "world.snapshot.json")
        self.lock_path = base / "world.lock"

    def _read_base(self) -> dict:
        if not self.snapshot_path.exists():
            return _empty_state()
        if self.snapshot_path.is_symlink():
            raise WorldStoreError(f"refusing symlink world snapshot: {self.snapshot_path}")
        state = _read_json(self.snapshot_path, WORLD_FILE_MAX_BYTES,
                           "world snapshot")
        return _validate_state_shape(state)

    def _read_events(self) -> list:
        if not self.wal_path.exists():
            return []
        if self.wal_path.is_symlink():
            raise WorldStoreError(f"refusing symlink world WAL: {self.wal_path}")
        try:
            if self.wal_path.stat().st_size > WORLD_FILE_MAX_BYTES:
                raise WorldStoreError("world WAL exceeds file limit")
            lines = self.wal_path.open("r", encoding="utf-8")
        except OSError as exc:
            raise WorldStoreError(f"cannot read world WAL: {exc}") from exc
        records = []
        with lines:
            for raw in lines:
                if len(raw.encode("utf-8", errors="replace")) > WORLD_LINE_MAX_BYTES:
                    raise WorldStoreError("world WAL contains oversized record")
                try:
                    records.append(_validate_event(json.loads(raw)))
                except (TypeError, ValueError) as exc:
                    if not raw.endswith("\n"):
                        break  # crash-truncated final append; snapshot remains usable
                    raise WorldStoreError(f"invalid world WAL record: {exc}") from exc
        return records

    def load(self) -> dict:
        """Load an atomic snapshot and apply only its subsequent WAL records."""
        state = self._read_base()
        expected = 1
        for record in self._read_events():
            sequence = record["event_sequence"]
            if sequence != expected:
                raise WorldStoreError("world WAL event_sequence has a gap or disorder")
            expected += 1
            if sequence <= state["event_sequence"]:
                continue
            if sequence != state["event_sequence"] + 1:
                raise WorldStoreError("world WAL cannot recover from a missing event")
            _apply_event(state, record)
        return state

    def current(self) -> Optional[dict]:
        """Return the latest persisted snapshot, or None before first observation."""
        snapshot = self.load().get("snapshot")
        return _json_copy(snapshot) if snapshot is not None else None

    def _commit_event(self, state: dict, record: dict) -> dict:
        _append_wal(self.wal_path, record)
        state = _apply_event(state, record)
        _atomic_json(self.snapshot_path, state)
        return state

    def record_snapshot(self, snapshot: dict, reason: str = "observe") -> dict:
        """Durably observe a new world; identical content is idempotent."""
        candidate = _validate_snapshot(snapshot)
        with _store_lock(self.lock_path):
            state = self.load()
            current = state.get("snapshot")
            if current and _snapshot_digest(current) == _snapshot_digest(candidate):
                return _json_copy(current)
            candidate["world_version"] = state["world_sequence"] + 1
            record = {
                "schema_version": WORLD_SCHEMA_VERSION,
                "event_sequence": state["event_sequence"] + 1,
                "world_sequence": candidate["world_version"],
                "kind": "world_snapshot", "snapshot": candidate,
                "reason": " ".join(str(reason).split())[:200],
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._commit_event(state, record)
            return _json_copy(candidate)

    def bind_plan(self, plan_id: str = "", snapshot: Optional[dict] = None) -> dict:
        """Persist a plan binding to the current monotonic world version."""
        if snapshot is not None:
            self.record_snapshot(snapshot, reason="plan_bind")
        with _store_lock(self.lock_path):
            state = self.load()
            current = state.get("snapshot")
            if current is None:
                raise WorldStoreError("cannot bind a plan before observing the world")
            identifier = str(plan_id).strip() or f"plan-{current['world_version']}"
            binding = plan_bind(current)
            binding["plan_id"] = identifier
            record = {
                "schema_version": WORLD_SCHEMA_VERSION,
                "event_sequence": state["event_sequence"] + 1,
                "kind": "plan_binding", "plan_id": identifier,
                "binding": binding,
                "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._commit_event(state, record)
            return _json_copy(binding)

    def reconcile(self, cwd: Optional[str] = None) -> dict:
        """Compare persisted state with a fresh observation and record changes."""
        actual = capture_world_snapshot(cwd or self.cwd)
        previous = self.current()
        if previous is None:
            current = self.record_snapshot(actual, reason="reconcile_initial")
            return {"changed": True, "reason": "initial_observation",
                    "snapshot": current, "drift": {}}
        drift = drift_between(previous, actual)
        if drift["version_changed"]:
            current = self.record_snapshot(actual, reason="reconcile_change")
            return {"changed": True, "reason": "world_changed",
                    "snapshot": current, "drift": drift}
        return {"changed": False, "reason": "unchanged",
                "snapshot": previous, "drift": drift}

    def status(self) -> dict:
        """Return bounded, JSON-serializable state-store health information."""
        state = self.load()
        return {"schema_version": state["schema_version"],
                "event_sequence": state["event_sequence"],
                "world_sequence": state["world_sequence"],
                "snapshot": state["snapshot"],
                "plan_count": len(state.get("plans", {})),
                "plans": state.get("plans", {}),
                "wal": str(self.wal_path),
                "snapshot_file": str(self.snapshot_path),
                "recovery": "validated_snapshot_plus_contiguous_wal"}


def _git(cwd: str, args: list) -> str:
    """只读 git 调用；非仓库或失败 → 空串（fail closed）。"""
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                                text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def capture_world_snapshot(cwd: str = "") -> dict:
    """当前世界快照：仓库版本 + 工作区变更 + fabric 协议版本 + 事实。"""
    root = cwd or "."
    head = _git(root, ["rev-parse", "HEAD"])
    dirty = _git(root, ["status", "--porcelain"])
    dirty_count = len([line for line in dirty.splitlines() if line.strip()])
    stable_facts = {
        "repository_head": head or "unknown",
        "dirty_files": dirty_count,
        "fabric_protocol": _module_versions(root),
    }
    fingerprint = hashlib.sha256(
        json.dumps(stable_facts, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]
    return {"version": fingerprint, **stable_facts,
            "as_of": time.strftime("%Y-%m-%dT%H:%M:%S")}


def _module_versions(root: str) -> str:
    """fabric 模块文件 mtime 摘要（版本变化 → 能力语义变化）。"""
    digest = hashlib.sha256()
    for rel in FABRIC_MODULES:
        path = Path(root) / rel
        try:
            digest.update(rel.encode())
            digest.update(str(path.stat().st_mtime_ns).encode())
        except OSError:
            digest.update(b"missing")
    return digest.hexdigest()[:12]


def plan_bind(snapshot: dict) -> dict:
    """把计划绑定到一个世界版本（执行前记录；version 保留原键供漂移对比）。"""
    world_version = snapshot.get("world_version", snapshot.get("version", ""))
    return {"version": snapshot.get("version", ""),
            "content_version": snapshot.get("content_version",
                                            snapshot.get("version", "")),
            "world_version": world_version,
            "plan_bound_world_version": world_version,
            "bound_at": snapshot.get("as_of", ""),
            "bound_repository_head": snapshot.get("repository_head", ""),
            "repository_head": snapshot.get("repository_head", ""),
            "dirty_files": snapshot.get("dirty_files", 0)}


def drift_between(bound: dict, current: dict,
                  threshold: int = 0) -> dict:
    """漂移检查：版本/HEAD/工作区变更数对比。超过阈值 → 需重新验证前提。"""
    head_changed = (bound.get("repository_head")
                    != current.get("repository_head"))
    dirty_delta = (_dirty_files(current) - _dirty_files(bound))
    bound_content = bound.get("content_version", bound.get("version"))
    current_content = current.get("content_version", current.get("version"))
    version_changed = bound_content != current_content
    world_version_changed = _world_version(bound) != _world_version(current)
    protocol_changed = (bound.get("fabric_protocol")
                        != current.get("fabric_protocol"))
    exceeds = version_changed and (
        head_changed or protocol_changed or abs(dirty_delta) > max(0, threshold))
    return {
        "version_changed": version_changed,
        "world_version_changed": world_version_changed,
        "head_changed": head_changed,
        "protocol_changed": protocol_changed,
        "dirty_delta": dirty_delta,
        "threshold": threshold,
        "exceeds_threshold": exceeds,
        "verdict": "replan_required" if exceeds else "proceed",
    }


def _dirty_files(snapshot: dict) -> int:
    try:
        value = int(snapshot.get("dirty_files", 0))
    except (TypeError, ValueError) as exc:
        raise WorldStoreError("world snapshot dirty_files must be an integer") from exc
    return max(0, value)


def _world_version(snapshot: dict):
    value = snapshot.get("world_version", snapshot.get("version", ""))
    return value


def _store_from_args(args) -> WorldStore:
    if getattr(args, "data_dir", ""):
        config.set_data_dir_override(args.data_dir)
    return WorldStore(getattr(args, "cwd", ""))


def _write_bound(path: Path, record: dict) -> None:
    _atomic_json(path, record)


def _bind_command(args) -> None:
    store = _store_from_args(args)
    snapshot = store.record_snapshot(capture_world_snapshot(args.cwd),
                                     reason="cli_bind")
    plan = store.bind_plan(args.plan_id or Path(args.out).stem)
    record = {"schema_version": WORLD_SCHEMA_VERSION, "snapshot": snapshot,
              "plan": plan, "store": {"wal": str(store.wal_path),
                                        "snapshot": str(store.snapshot_path)}}
    _write_bound(Path(args.out), record)
    print(f"bound world version {snapshot['world_version']} → {args.out}")
    raise SystemExit(0)


def _read_bound(path: str) -> dict:
    try:
        value = _read_json(Path(path), config.INPUT_MAX_BYTES, "bound world snapshot")
    except WorldStoreError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
    if not isinstance(value, dict):
        print("bound world snapshot must be an object", file=sys.stderr)
        raise SystemExit(2)
    return value


def _drift_command(args) -> None:
    store = _store_from_args(args)
    bound = _read_bound(args.bound)
    reconciliation = store.reconcile(args.cwd)
    current = reconciliation["snapshot"]
    drift = drift_between(bound.get("snapshot", bound), current,
                          threshold=args.threshold)
    report = {"bound": bound.get("snapshot", bound), "current": current,
              "drift": drift, "reconciliation": reconciliation}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        verdict = drift["verdict"]
        print(f"world drift: {verdict}"
              f"（head_changed={drift['head_changed']} "
              f"dirty_delta={drift['dirty_delta']} "
              f"threshold={args.threshold}）")
        if verdict == "replan_required":
            print("⚠ 世界状态已变化：不能机械执行旧计划，必须重新验证前提")
    raise SystemExit(1 if drift["exceeds_threshold"] else 0)


def _reconcile_command(args) -> None:
    report = _store_from_args(args).reconcile(args.cwd)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"world reconcile: {report['reason']} "
              f"(world_version={report['snapshot'].get('world_version', 0)})")
    raise SystemExit(0)


def _status_command(args) -> None:
    report = _store_from_args(args).status()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"world status: version={report['world_sequence']} "
              f"events={report['event_sequence']} plans={report['plan_count']}")
    raise SystemExit(0)


def world_main(argv: Optional[list] = None) -> int:
    """`pi-batch world bind|drift|status|reconcile`。"""
    parser = argparse.ArgumentParser(
        prog="pi-batch.py world",
        description="World state plane: versioned snapshot + drift")
    sub = parser.add_subparsers(dest="command", required=True)

    p_bind = sub.add_parser("bind", help="绑定当前世界版本到文件")
    p_bind.add_argument("--out", required=True)
    p_bind.add_argument("--plan-id", default="")
    p_bind.add_argument("--cwd", default="")
    p_bind.add_argument("--data-dir", default="")

    p_drift = sub.add_parser("drift", help="检查当前与绑定版本的漂移")
    p_drift.add_argument("--bound", required=True, help="bind 输出的文件")
    p_drift.add_argument("--threshold", type=int, default=0)
    p_drift.add_argument("--cwd", default="")
    p_drift.add_argument("--data-dir", default="")
    p_drift.add_argument("--json", action="store_true")

    for name, help_text in (("status", "查看持久化世界状态"),
                            ("reconcile", "观测并持久化当前世界")):
        command = sub.add_parser(name, help=help_text)
        command.add_argument("--cwd", default="")
        command.add_argument("--data-dir", default="")
        command.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "bind":
        _bind_command(args)
    if args.command == "drift":
        _drift_command(args)
    if args.command == "status":
        _status_command(args)
    _reconcile_command(args)
