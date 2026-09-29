"""Bounded private checkpoints for resumable meta-stage role plans."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from . import config, file_lock
from .text_io import read_text_bounded

_SCHEMA = 1
_MAX_STATE_BYTES = 256 * 1024
_MAX_ROLES = 64
_MAX_ITERATIONS = 128
_HEX = frozenset("0123456789abcdef")


class MetaResumeStore:
    """Persist only validated named-role plans, never prompt or task text."""

    def __init__(self, stage, *, model_override: str, validate_cmd: str,
                 original_prompt: str, session_name: str, role_names: list,
                 timeout_override: int = 0, fingerprint_mode: bool = False):
        cwd = Path(stage.cwd or os.getcwd()).expanduser().resolve()
        output = Path(stage.output_dir).expanduser()
        if not output.is_absolute():
            output = cwd / output
        identity = _digest({"cwd": str(cwd), "stage": stage.name,
                            "output": str(output.resolve())})
        self.path = config.data_dir(str(cwd)) / "meta" / "resume" / f"{identity}.json"
        self.signature = _stage_signature(
            stage, model_override, validate_cmd, original_prompt,
            session_name, role_names, timeout_override, fingerprint_mode)
        self._state = _empty_state(self.signature)

    def load(self, *, resume: bool) -> None:
        """Load a matching checkpoint or start a fresh run generation."""
        if resume:
            candidate = _read_state(self.path)
            if candidate and candidate.get("signature") == self.signature:
                self._state = candidate
                return
        self._state = _empty_state(self.signature)
        self._save()

    def iteration(self, number: int, evidence_digest: str) -> dict | None:
        record = self._state["iterations"].get(str(number))
        if (not isinstance(record, dict)
                or record.get("evidence") != evidence_digest
                or record.get("resumable") is not True):
            return None
        return dict(record)

    def planned(self, number: int, evidence_digest: str, roles: list) -> None:
        """Checkpoint named-role choice before any worker process is launched."""
        resumable = _named_roles_only(roles)
        record = {
            "evidence": evidence_digest,
            "roles": [item["role"] for item in roles] if resumable else [],
            "resumable": resumable,
            "status": "PLANNED" if resumable else "NON_RESUMABLE",
            "done": not roles,
        }
        self._state["iterations"][str(number)] = record
        if roles and not resumable:
            config.log.info("META resume: ad-hoc plan not cached for iteration %d", number)
        self._trim_iterations()
        self._save()

    def started(self, number: int) -> None:
        self._set_status(number, "RUNNING")

    def completed(self, number: int, *, success: bool) -> None:
        self._set_status(number, "COMPLETED" if success else "FAILED")

    def _set_status(self, number: int, status: str) -> None:
        record = self._state["iterations"].get(str(number))
        if not isinstance(record, dict) or record.get("resumable") is not True:
            return
        record["status"] = status
        self._save()

    def _trim_iterations(self) -> None:
        values = self._state["iterations"]
        if len(values) <= _MAX_ITERATIONS:
            return
        for key in sorted(values, key=int)[:-_MAX_ITERATIONS]:
            del values[key]

    def _save(self) -> None:
        payload = _encode_state(self._state)
        _atomic_write(self.path, payload)


def evidence_fingerprint(combined: str) -> str:
    """Hash the exact bounded evidence sent to the planner without storing it."""
    return hashlib.sha256(combined.encode("utf-8", errors="replace")).hexdigest()


def _stage_signature(stage, model_override: str, validate_cmd: str,
                     original_prompt: str, session_name: str,
                     role_names: list, timeout_override: int,
                     fingerprint_mode: bool) -> str:
    values = {
        "name": stage.name, "cwd": str(Path(stage.cwd or os.getcwd()).resolve()),
        "role_names": role_names, "templates": _template_hashes(stage.role_dir),
        "meta_prompt": stage.meta_prompt, "original_prompt": original_prompt,
        "keyword_content": _file_hash(stage.role_keywords),
        "output_dir": stage.output_dir, "iterations": stage.max_iterations,
        "workers": stage.workers, "timeout": [timeout_override, stage.meta_timeout],
        "fingerprint_mode": fingerprint_mode,
        "planner": [model_override or stage.orchestrator_model or stage.model
                    or config.AGENT_DEFAULT_MODEL,
                    stage.orchestrator_provider or stage.provider,
                    stage.agent or config.AGENT_BIN,
                    config.agent_no_tools_args(stage.agent or config.AGENT_BIN)
                    if stage.delegation_only else []],
        "worker": [model_override or stage.worker_model or stage.model
                   or config.AGENT_DEFAULT_MODEL,
                   stage.worker_provider or stage.provider,
                   stage.role_models, stage.role_providers],
        "validation": validate_cmd, "session": session_name,
        "max_failed_roles": stage.meta_max_failed_roles,
        "role_retries": stage.meta_role_retries,
        "delegation_only": stage.delegation_only,
        "max_roles": stage.max_roles_per_iteration,
        "task_dag": stage.meta_task_dag,
        "relevance": [stage.relevance_enabled, stage.relevance_min_score],
    }
    return _digest(values)


def _template_hashes(role_dir: str) -> dict[str, str]:
    if not role_dir:
        return {}
    root = Path(role_dir)
    if not root.is_dir():
        root = Path(config.resolve_asset_path(role_dir))
    if not root.is_dir():
        return {}
    values = {}
    for path in sorted(root.glob("*.md"))[:128]:
        if path.stem == "README":
            continue
        try:
            text = read_text_bounded(path, config.INPUT_MAX_BYTES, "role template")
        except (OSError, ValueError):
            values[path.stem] = "unreadable"
        else:
            values[path.stem] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return values


def _file_hash(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if not path.is_file():
        path = Path(config.resolve_asset_path(value))
    try:
        text = read_text_bounded(path, config.INPUT_MAX_BYTES, "role keywords")
    except (OSError, ValueError):
        return "unreadable"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _named_roles_only(roles: list) -> bool:
    return (len(roles) <= _MAX_ROLES and all(
        isinstance(item, dict) and isinstance(item.get("role"), str)
        and item["role"] and not item.get("task")
        and "id" not in item and not item.get("depends_on")
        and len(item["role"].encode("utf-8")) <= 256 for item in roles))


def _empty_state(signature: str) -> dict:
    return {"schema": _SCHEMA, "signature": signature, "iterations": {}}


def _read_state(path: Path) -> dict | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            raw = stream.read(_MAX_STATE_BYTES + 1)
        if len(raw) > _MAX_STATE_BYTES:
            return None
        value = json.loads(raw.decode("utf-8"))
        return value if _valid_state(value) else None
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def _valid_state(value: object) -> bool:
    if (not isinstance(value, dict) or set(value) != {"schema", "signature", "iterations"}
            or isinstance(value.get("schema"), bool) or value.get("schema") != _SCHEMA
            or not _is_digest(value.get("signature"))
            or not isinstance(value.get("iterations"), dict)):
        return False
    iterations = value["iterations"]
    if len(iterations) > _MAX_ITERATIONS:
        return False
    return all(_valid_iteration(key, item) for key, item in iterations.items())


def _valid_iteration(key: object, value: object) -> bool:
    return _valid_iteration_key(key) and _valid_iteration_record(value)


def _valid_iteration_key(key: object) -> bool:
    if not isinstance(key, str) or not key.isdigit():
        return False
    number = int(key)
    return str(number) == key and 1 <= number <= _MAX_ITERATIONS


def _valid_iteration_record(value: object) -> bool:
    fields = {"evidence", "roles", "resumable", "status", "done"}
    if not isinstance(value, dict) or set(value) != fields:
        return False
    roles = value.get("roles")
    if not _valid_role_names(roles) or not _is_digest(value.get("evidence")):
        return False
    resumable, status, done = (value["resumable"], value["status"], value["done"])
    if not isinstance(resumable, bool) or not isinstance(done, bool):
        return False
    if status not in {"PLANNED", "RUNNING", "COMPLETED", "FAILED",
                      "NON_RESUMABLE"}:
        return False
    return _valid_iteration_status(resumable, status, done, roles)


def _valid_role_names(roles: object) -> bool:
    if not isinstance(roles, list) or len(roles) > _MAX_ROLES:
        return False
    return all(isinstance(role, str) and role
               and len(role.encode("utf-8")) <= 256 for role in roles)


def _valid_iteration_status(resumable: bool, status: str,
                            done: bool, roles: list) -> bool:
    if resumable:
        return status != "NON_RESUMABLE" and done == (not roles)
    return status == "NON_RESUMABLE" and not done and not roles


def _encode_state(value: dict) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(payload) > _MAX_STATE_BYTES:
        raise ValueError("meta resume state exceeds size limit")
    return payload


def _digest(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _is_digest(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in _HEX for char in value))


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        file_lock.flock(descriptor, file_lock.LOCK_EX)
        _replace_file(path, payload)
    finally:
        os.close(descriptor)


def _replace_file(path: Path, payload: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass
