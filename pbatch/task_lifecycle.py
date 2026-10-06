"""Durable pipeline checkpoints and scoped parent identities, never control.

Every checkpoint contains the bounded plan and all observed stage outcomes,
so losing the beginning of a JSONL tail does not invent a remaining DAG.
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import config, metering
from .task_center_io import display
from .task_center_metrics import number, timestamp
from .task_state_io import append_bounded, atomic_write, load, locked

RUN_PATTERN = re.compile(r"pl-[a-f0-9]{32}\Z")
MAX_STAGES = 64
MAX_RUNS = 1024
_SCOPE = contextvars.ContextVar("task_center_pipeline", default=None)
_ACTIVE = {}
_LOCK = threading.RLock()


def source_identity(root: Path) -> str:
    return "src-" + hashlib.sha256(str(root).encode()).hexdigest()[:24]


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_path(source: dict, run_id: str) -> Path:
    if not isinstance(run_id, str) or not RUN_PATTERN.fullmatch(run_id):
        raise ValueError("invalid pipeline run identity")
    return Path(source["data_dir"]) / "tasks" / "runs" / (run_id + ".json")


def validate_run(value: dict) -> dict:
    required = {"schema_version", "run_id", "source_id", "kind", "name", "status",
                "started_at", "ts", "seq", "plan_complete", "steps", "stage",
                "parent_run_id", "parent_source_id", "process_run_id"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("invalid pipeline checkpoint fields")
    _validate_header(value)
    if not isinstance(value["steps"], list) or len(value["steps"]) > MAX_STAGES:
        raise ValueError("invalid pipeline plan size")
    for step in value["steps"]:
        _validate_step(step)
    names = [step["id"] for step in value["steps"]]
    if len(set(names)) != len(names):
        raise ValueError("duplicate plan stage")
    _validate_order(value, names)
    if value["plan_complete"] and value["status"] == "succeeded" and any(
            step["state"] != "succeeded" for step in value["steps"]):
        raise ValueError("successful checkpoint has incomplete stages")
    return value


def _validate_order(value: dict, names: list) -> None:
    states = {step["id"]: step["state"] for step in value["steps"]}
    if value["plan_complete"] and value["stage"] and value["stage"] not in names:
        raise ValueError("checkpoint stage is outside its declared plan")
    for index, step in enumerate(value["steps"]):
        if any(dep not in names[:index] for dep in step["dependencies"]):
            raise ValueError("invalid plan dependency")
        if step["state"] != "pending" and any(states[dep] != "succeeded" for dep in step["dependencies"]):
            raise ValueError("stage outcome precedes successful dependencies")
    if sum(step["state"] == "running" for step in value["steps"]) > 1:
        raise ValueError("multiple running stages in a sequential pipeline")


def _validate_header(value: dict) -> None:
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("unsupported pipeline checkpoint schema")
    _validate_identities(value)
    if type(value["seq"]) is not int or value["seq"] < 1:
        raise ValueError("invalid checkpoint sequence")
    if not isinstance(value["status"], str) or value["status"] not in {
            "running", "succeeded", "failed", "cancelled", "interrupted"}:
        raise ValueError("invalid checkpoint status")
    if type(value["plan_complete"]) is not bool:
        raise ValueError("invalid plan coverage")
    if number(value["started_at"]) is None or timestamp(value["ts"]) is None:
        raise ValueError("invalid checkpoint time")
    if not isinstance(value["ts"], str) or len(value["ts"]) > 64:
        raise ValueError("invalid checkpoint timestamp")


def _validate_identities(value: dict) -> None:
    if value["kind"] != "pipeline" or not isinstance(value["run_id"], str):
        raise ValueError("invalid checkpoint kind/identity")
    if not RUN_PATTERN.fullmatch(value["run_id"]):
        raise ValueError("invalid checkpoint run identity")
    if not isinstance(value["source_id"], str) or not re.fullmatch(r"src-[a-f0-9]{24}", value["source_id"]):
        raise ValueError("invalid checkpoint source")
    if not all(isinstance(value[key], str) and len(value[key]) <= maximum
               for key, maximum in (("name", 240), ("stage", 160), ("process_run_id", 160),
                                     ("parent_run_id", 64), ("parent_source_id", 64))):
        raise ValueError("invalid checkpoint text field")
    if value["parent_run_id"] or value["parent_source_id"]:
        if (not RUN_PATTERN.fullmatch(value["parent_run_id"])
                or not re.fullmatch(r"src-[a-f0-9]{24}", value["parent_source_id"])):
            raise ValueError("incomplete parent identity")


def _validate_step(step: dict) -> None:
    if not isinstance(step, dict) or set(step) != {"id", "dependencies", "state", "gate"}:
        raise ValueError("invalid plan step")
    if not isinstance(step["id"], str) or not 1 <= len(step["id"]) <= 160:
        raise ValueError("invalid plan stage identity")
    if not isinstance(step["state"], str) or step["state"] not in {
            "pending", "running", "succeeded", "failed", "cancelled"}:
        raise ValueError("invalid stage state")
    if not isinstance(step["dependencies"], list) or len(step["dependencies"]) > MAX_STAGES:
        raise ValueError("invalid dependency list")
    if not all(isinstance(dep, str) for dep in step["dependencies"]):
        raise ValueError("invalid dependency identity")
    if type(step["gate"]) is not bool:
        raise ValueError("invalid gate marker")


def load_run(source: dict, run_id: str) -> dict | None:
    value = load(run_path(source, run_id))
    if value is not None:
        validate_run(value)
        if value["run_id"] != run_id or value["source_id"] != source["source_id"]:
            raise ValueError("checkpoint source/identity mismatch")
    return value


def _plan(pipeline) -> tuple[list, bool]:
    steps = []
    stages = pipeline.stages
    for stage in stages[:MAX_STAGES]:
        dependency = stage.from_outputs
        dependencies = dependency if isinstance(dependency, list) else [dependency] if dependency else []
        previous = [steps[-1]["id"]] if steps else []
        steps.append({"id": stage.name, "dependencies": list(dict.fromkeys(
            [*previous, *dependencies])), "state": "pending", "gate": bool(stage.gate)})
    return steps, len(stages) <= MAX_STAGES


class PipelineFacts:
    def __init__(self, pipeline, cwd: str):
        self.root = config.data_dir(cwd).absolute()
        self.enabled = config.AGENT_LEDGER_ENABLED
        steps, complete = _plan(pipeline)
        parent = _SCOPE.get()
        self.state = {"schema_version": 1, "kind": "pipeline", "run_id": "pl-" + uuid.uuid4().hex,
            "source_id": source_identity(self.root), "name": display(pipeline.name, 240),
            "status": "running", "started_at": time.time(), "ts": stamp(), "seq": 0,
            "plan_complete": complete, "steps": steps, "stage": "",
            "parent_run_id": parent.state["run_id"] if parent else "",
            "parent_source_id": parent.state["source_id"] if parent else "",
            "process_run_id": metering.RUN_ID}

    def publish(self) -> None:
        if not self.enabled:
            return
        try:
            self.state["seq"] += 1
            self.state["ts"] = stamp()
            validate_run(self.state)
            path = run_path({"data_dir": str(self.root)}, self.state["run_id"])
            with locked(path):
                previous = load(path)
                if previous is None:
                    if self.state["seq"] != 1:
                        raise ValueError("pipeline checkpoint disappeared")
                    _check_run_capacity(path.parent)
                elif validate_run(previous)["seq"] != self.state["seq"] - 1:
                    raise ValueError("pipeline checkpoint sequence conflict")
                atomic_write(path, self.state)
            append_bounded(self.root / "tasks" / "lifecycle.jsonl", self.state)
        except Exception as exc:
            self.enabled = False
            config.log.warning("Task lifecycle observation disabled: %s", display(str(exc)))

    def stage_started(self, name: str) -> None:
        self.state["stage"] = name
        self._stage(name, "running")

    def _stage(self, name: str, status: str) -> None:
        for step in self.state["steps"]:
            if step["id"] == name:
                step["state"] = status
        self.publish()

    def stage_completed(self, name: str, success: bool, cancelled: bool) -> None:
        self._stage(name, "cancelled" if cancelled else "succeeded" if success else "failed")

    def finish(self, finished: bool, failed: list, cancelled: bool) -> None:
        self.state["status"] = ("cancelled" if cancelled else "interrupted" if not finished
                                else "failed" if failed else "succeeded")
        self.publish()


def _check_run_capacity(directory: Path) -> None:
    for index, _ in enumerate(directory.iterdir()):
        if index >= MAX_RUNS * 2:
            raise ValueError("pipeline checkpoint capacity reached")


def open_scope(pipeline, cwd: str, dry_run: bool):
    if dry_run or not config.AGENT_LEDGER_ENABLED:
        return None, None
    facts, token = None, None
    try:
        facts = PipelineFacts(pipeline, cwd)
        token = _SCOPE.set(facts)
        with _LOCK:
            _ACTIVE[facts.state["run_id"]] = facts
        facts.publish()
        return facts, token
    except Exception as exc:
        if token is not None:
            with _LOCK:
                _ACTIVE.pop(facts.state["run_id"], None)
            _SCOPE.reset(token)
        config.log.warning("Task lifecycle unavailable: %s", display(str(exc)))
        return None, None


def close_scope(facts, token, finished: bool, failed: list, cancelled: bool) -> None:
    if facts is None:
        return
    try:
        notify(facts, "finish", finished, failed, cancelled)
    except Exception as exc:
        config.log.warning("Task observation close failed: %s", type(exc).__name__)
    finally:
        with _LOCK:
            _ACTIVE.pop(facts.state["run_id"], None)
        _SCOPE.reset(token)


def notify(facts, method: str, *args) -> None:
    if facts is not None:
        try:
            getattr(facts, method)(*args)
        except Exception as exc:
            facts.enabled = False
            config.log.warning("Task checkpoint observation lost: %s", type(exc).__name__)


def bind_tasks(tasks: list) -> None:
    facts = _SCOPE.get()
    if facts is not None:
        for task in tasks:
            task.memory["_task_center_pipeline"] = facts.state["run_id"]


def parent_fields(task) -> dict:
    seed = task.memory.get("_task_center_pipeline")
    with _LOCK:
        facts = _SCOPE.get() or (_ACTIVE.get(seed) if isinstance(seed, str) else None)
    if facts is None:
        return {}
    return {"parent_run_id": facts.state["run_id"],
            "parent_source_id": facts.state["source_id"]}
