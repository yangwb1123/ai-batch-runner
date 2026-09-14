"""NVIDIA CUDA physical-GPU discovery and assignment validation.

This module reports visibility and advisory memory availability. CUDA
visibility is not a hardware isolation boundary.
"""

from __future__ import annotations

import csv
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import time

MAX_GPU_COUNT = 16
MAX_GPU_MEMORY_BYTES = 1024 ** 4
MAX_GPU_TIMESTAMP = 10 ** 12
GPU_PROBE_TIMEOUT_SECONDS = 2.0
GPU_PROBE_OUTPUT_BYTES = 65536
GPU_OBSERVATION_TTL_SECONDS = 90.0
MAX_GPU_DEVICES = MAX_GPU_COUNT
GPU_UUID_RE = re.compile(
    r"^GPU-[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
GPU_PROBE_FIELDS = "uuid,name,memory.total,memory.free,mig.mode.current"


def normalize_gpu_resources(resources: dict) -> dict:
    """Return canonical bounded GPU fields while preserving CPU-only shape."""
    if not isinstance(resources, dict):
        raise ValueError("resources must be an object")
    unknown = set(resources) - {
        "cpu_cores", "memory_bytes", "gpu_count", "gpu_memory_bytes"}
    if unknown:
        raise ValueError("resources contains unsupported fields")
    count = _bounded_integer(resources.get("gpu_count", 0), "gpu_count",
                            MAX_GPU_COUNT, 0)
    memory = _bounded_integer(resources.get("gpu_memory_bytes", 0),
                              "gpu_memory_bytes", MAX_GPU_MEMORY_BYTES, 0)
    result = {key: value for key, value in resources.items()
              if key in ("cpu_cores", "memory_bytes")}
    if count == 0:
        if memory:
            raise ValueError("resources.gpu_memory_bytes requires gpu_count")
        return result
    result.update({"gpu_count": count, "gpu_memory_bytes": memory})
    return result


def _bounded_integer(value, name: str, maximum: int, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"resources.{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"resources.{name} is outside supported bounds")
    return value


def normalize_gpu_assignment(assignment: dict, gpu_count: int,
                             gpu_memory_bytes: int) -> dict:
    """Validate the durable server-selected physical-GPU assignment DTO."""
    fields = {"vendor", "mode", "uuids", "gpu_memory_bytes",
              "assigned_at", "observed_at"}
    if not isinstance(assignment, dict) or set(assignment) != fields:
        raise ValueError("gpu_assignment has an invalid shape")
    if assignment["vendor"] != "nvidia" or assignment["mode"] != "physical":
        raise ValueError("gpu_assignment must name NVIDIA physical GPUs")
    count = gpu_count
    if (isinstance(count, bool) or not isinstance(count, int)
            or not 1 <= count <= MAX_GPU_COUNT):
        raise ValueError("gpu assignment request count is invalid")
    memory = _bounded_integer(assignment["gpu_memory_bytes"],
                              "gpu_memory_bytes", MAX_GPU_MEMORY_BYTES, 0)
    required_memory = _bounded_integer(
        gpu_memory_bytes, "gpu_memory_bytes", MAX_GPU_MEMORY_BYTES, 0)
    if memory != required_memory:
        raise ValueError("gpu_assignment memory does not match request")
    uuids = assignment["uuids"]
    if (not isinstance(uuids, list) or len(uuids) != count
            or any(not isinstance(value, str)
                   or not GPU_UUID_RE.fullmatch(value) for value in uuids)
            or len({value.casefold() for value in uuids}) != count):
        raise ValueError("gpu_assignment contains invalid GPU UUIDs")
    assigned_at = _finite_timestamp(assignment["assigned_at"], "assigned_at")
    observed_at = _finite_timestamp(assignment["observed_at"], "observed_at")
    if observed_at > assigned_at:
        raise ValueError("gpu_assignment observation is after assignment")
    return {"vendor": "nvidia", "mode": "physical",
            "uuids": list(uuids), "gpu_memory_bytes": memory,
            "assigned_at": assigned_at, "observed_at": observed_at}


def _finite_timestamp(value, name: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value <= 0 or value > MAX_GPU_TIMESTAMP):
        raise ValueError(f"gpu_assignment.{name} must be a timestamp")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"gpu_assignment.{name} must be a timestamp") from None
    if not math.isfinite(result) or not 0 < result <= MAX_GPU_TIMESTAMP:
        raise ValueError(f"gpu_assignment.{name} must be a positive timestamp")
    return result


def gpu_observation_fresh(value, now: float,
                          max_age_seconds: float = GPU_OBSERVATION_TTL_SECONDS
                          ) -> bool:
    try:
        observed = normalize_gpu_timestamp(value)
        if observed is None:
            return False
        current = float(now)
        age = current - observed
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(age) and -5.0 <= age <= max_age_seconds


def normalize_gpu_timestamp(value) -> float | None:
    """Return a bounded finite GPU observation timestamp, if valid."""
    try:
        return _finite_timestamp(value, "observed_at")
    except ValueError:
        return None


def probe_nvidia_gpus(timeout: float = GPU_PROBE_TIMEOUT_SECONDS,
                      env: dict | None = None,
                      popen_factory=subprocess.Popen) -> dict:
    """Probe NVIDIA physical GPUs using bounded argv-only nvidia-smi."""
    source_env = dict(os.environ if env is None else env)
    executable = shutil.which("nvidia-smi", path=source_env.get("PATH"))
    observed_at = time.time()
    if not executable:
        return _probe_result("unavailable", [], observed_at)
    probe_env = dict(source_env)
    probe_env["LC_ALL"] = "C"
    try:
        code, output = _run_bounded_probe(
            [executable, f"--query-gpu={GPU_PROBE_FIELDS}",
             "--format=csv,noheader,nounits"], probe_env,
            _bounded_timeout(timeout), popen_factory)
    except (OSError, ValueError, subprocess.SubprocessError):
        return _probe_result("unavailable", [], observed_at)
    if code != 0:
        return _probe_result("unavailable", [], observed_at)
    devices = _parse_gpu_rows(output, observed_at)
    devices, restricted = _respect_visible_devices(
        devices, source_env.get("CUDA_VISIBLE_DEVICES"))
    status = ("available" if any(row["status"] == "available"
                                  for row in devices) else "unavailable")
    if restricted and not devices:
        status = "restricted"
    return _probe_result(status, devices, observed_at)


def _bounded_timeout(timeout: float) -> float:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return GPU_PROBE_TIMEOUT_SECONDS
    if not math.isfinite(value):
        return GPU_PROBE_TIMEOUT_SECONDS
    return min(5.0, max(0.1, value))


def _run_bounded_probe(argv: list, env: dict, timeout: float,
                       popen_factory) -> tuple[int, bytes]:
    proc = popen_factory(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, shell=False, close_fds=True,
        start_new_session=True, env=env)
    selector = selectors.DefaultSelector()
    output = bytearray()
    stream = proc.stdout
    try:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
        _collect_probe_output(proc, stream, selector, output, timeout)
        if proc.poll() is None:
            _kill_probe_group(proc)
        try:
            return proc.wait(timeout=1.0), bytes(output)
        except subprocess.TimeoutExpired:
            _kill_probe_group(proc)
            return proc.wait(), bytes(output)
    except (OSError, ValueError, subprocess.SubprocessError):
        _reap_probe_process(proc)
        raise
    finally:
        selector.close()
        if stream is not None:
            stream.close()


def _collect_probe_output(proc, stream, selector, output: bytearray,
                          timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _kill_probe_group(proc)
            raise subprocess.TimeoutExpired(proc.args, timeout)
        events = selector.select(min(0.1, remaining))
        for key, _ in events:
            chunk = os.read(key.fd, 8192)
            if not chunk:
                selector.unregister(key.fileobj)
                continue
            if len(output) + len(chunk) > GPU_PROBE_OUTPUT_BYTES:
                _kill_probe_group(proc)
                raise ValueError("nvidia-smi output exceeds limit")
            output.extend(chunk)
    if proc.poll() is None:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            proc.wait(timeout=remaining)
        except subprocess.TimeoutExpired as exc:
            _kill_probe_group(proc)
            raise subprocess.TimeoutExpired(proc.args, timeout) from exc


def _kill_probe_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass


def _reap_probe_process(proc) -> None:
    if proc.poll() is not None:
        return
    _kill_probe_group(proc)
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        _kill_probe_group(proc)
        proc.wait()


def _parse_gpu_rows(output: bytes, observed_at: float) -> list[dict]:
    try:
        text = output.decode("utf-8", errors="strict")
        rows = list(csv.reader(text.splitlines(), strict=True))
    except (UnicodeDecodeError, csv.Error):
        return []
    if len(rows) > MAX_GPU_DEVICES:
        return []
    devices = []
    for row in rows:
        device = _parse_gpu_row(row, observed_at)
        if device is not None:
            devices.append(device)
    uuids = [row["uuid"].casefold() for row in devices]
    if len(uuids) != len(set(uuids)):
        return []
    return devices


def _parse_gpu_row(row: list[str], observed_at: float) -> dict | None:
    if len(row) != 5:
        return None
    uuid, name, total, available, mig_mode = (item.strip() for item in row)
    if not _valid_gpu_name(name) or not GPU_UUID_RE.fullmatch(uuid):
        return None
    total_bytes, available_bytes = _memory_bytes(total), _memory_bytes(available)
    if (total_bytes is None or available_bytes is None
            or available_bytes > total_bytes):
        return None
    mode = mig_mode.lower()
    if mode not in ("enabled", "disabled", "n/a"):
        return None
    status = "mig_unsupported" if mode == "enabled" else "available"
    return {"uuid": uuid, "name": name, "vendor": "nvidia",
            "total_memory_bytes": total_bytes,
            "available_memory_bytes": available_bytes,
            "mig_mode": mode, "status": status,
            "observed_at": observed_at}


def _memory_bytes(value: str) -> int | None:
    try:
        mebibytes = int(value.strip())
    except (TypeError, ValueError):
        return None
    if mebibytes < 0 or mebibytes > MAX_GPU_MEMORY_BYTES // (1024 ** 2):
        return None
    return mebibytes * 1024 ** 2


def _valid_gpu_name(name: str) -> bool:
    if not name or len(name) > 128 or any(ord(char) < 32 or ord(char) == 127
                                           for char in name):
        return False
    try:
        name.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _respect_visible_devices(devices: list, value) -> tuple[list, bool]:
    if value is None:
        return devices, False
    text = str(value)
    if not text or text == "-1":
        return [], True
    tokens = [token.strip() for token in text.split(",")]
    if any(not token for token in tokens):
        return [], True
    selected = []
    for token in tokens:
        device = _visible_device(token, devices)
        if device is None:
            return [], True
        if device not in selected:
            selected.append(device)
    return selected, True


def _visible_device(token: str, devices: list) -> dict | None:
    if GPU_UUID_RE.fullmatch(token):
        matches = [device for device in devices if device["uuid"] == token]
        return matches[0] if len(matches) == 1 else None
    return None


def _probe_result(status: str, devices: list, observed_at: float) -> dict:
    return {"status": status, "devices": devices,
            "observed_at": observed_at}


def select_gpu_assignment(request: dict, gpu_rows: list,
                          reserved_uuids: set | list | tuple,
                          assigned_at: float) -> dict | None:
    """Pick stable UUIDs from fresh schedulable inventory rows."""
    count = request.get("gpu_count", 0)
    minimum = request.get("gpu_memory_bytes", 0)
    if (isinstance(count, bool) or not isinstance(count, int)
            or not 1 <= count <= MAX_GPU_COUNT
            or isinstance(minimum, bool) or not isinstance(minimum, int)
            or not 0 <= minimum <= MAX_GPU_MEMORY_BYTES):
        return None
    reserved = set(reserved_uuids)
    candidates = [row for row in gpu_rows if _gpu_row_schedulable(
        row, minimum, reserved, assigned_at)
        and row["observed_at"] <= assigned_at]
    candidates.sort(key=lambda row: row["uuid"])
    if len(candidates) < count:
        return None
    assignment = {"vendor": "nvidia", "mode": "physical",
                  "uuids": [row["uuid"] for row in candidates[:count]],
                  "gpu_memory_bytes": minimum,
                  "assigned_at": assigned_at,
                  "observed_at": min(row["observed_at"]
                                     for row in candidates[:count])}
    try:
        return normalize_gpu_assignment(assignment, count, minimum)
    except ValueError:
        return None


def _gpu_row_schedulable(row: dict, minimum: int, reserved: set,
                         now: float) -> bool:
    if not isinstance(row, dict):
        return False
    uuid = row.get("uuid")
    reserved_keys = {item.casefold() for item in reserved
                     if isinstance(item, str)}
    if (row.get("vendor") != "nvidia" or not isinstance(uuid, str)
            or not GPU_UUID_RE.fullmatch(uuid)
            or uuid.casefold() in reserved_keys
            or row.get("schedulable") is not True
            or row.get("reserved") is True
            or not gpu_observation_fresh(row.get("observed_at"), now)):
        return False
    total, available = row.get("total_memory_bytes"), row.get(
        "available_memory_bytes")
    return (_is_bounded_memory(total) and total > 0
            and _is_bounded_memory(available) and available <= total
            and available >= minimum)


def _is_bounded_memory(value) -> bool:
    return (not isinstance(value, bool) and isinstance(value, int)
            and 0 <= value <= MAX_GPU_MEMORY_BYTES)


def _unique_gpu_map(rows: list) -> dict:
    result = {}
    duplicates = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        uuid = row.get("uuid")
        if not isinstance(uuid, str) or not GPU_UUID_RE.fullmatch(uuid):
            continue
        key = uuid.casefold()
        if key in result:
            duplicates.add(key)
        result[key] = row
    return {uuid: row for uuid, row in result.items()
            if uuid not in duplicates}


def verify_gpu_assignment(assignment: dict, fresh_probe: dict,
                          gpu_count: int, gpu_memory_bytes: int,
                          now: float | None = None,
                          max_age_seconds: float = GPU_OBSERVATION_TTL_SECONDS
                          ) -> str:
    """Reject stale, missing, MIG, or under-capacity local GPU assignments."""
    try:
        expected = normalize_gpu_assignment(
            assignment, gpu_count, gpu_memory_bytes)
    except ValueError as exc:
        return str(exc)
    if not isinstance(fresh_probe, dict) or fresh_probe.get("status") != "available":
        return "fresh NVIDIA GPU probe is unavailable"
    observed = fresh_probe.get("observed_at")
    current = time.time() if now is None else now
    if not gpu_observation_fresh(observed, current, max_age_seconds):
        return "fresh NVIDIA GPU probe is stale"
    rows = fresh_probe.get("devices")
    if not isinstance(rows, list):
        return "fresh NVIDIA GPU probe is malformed"
    by_uuid = _unique_gpu_map(rows)
    for uuid in expected["uuids"]:
        row = by_uuid.get(uuid.casefold())
        if (row is None or row.get("status") != "available"
                or row.get("vendor") != "nvidia"):
            return "assigned NVIDIA GPU UUID is unavailable"
        if not gpu_observation_fresh(row.get("observed_at"), current,
                                     max_age_seconds):
            return "assigned NVIDIA GPU observation is stale"
        total = row.get("total_memory_bytes")
        if not _is_bounded_memory(total) or total == 0:
            return "assigned NVIDIA GPU total memory is invalid"
        available = row.get("available_memory_bytes")
        if not _is_bounded_memory(available) or available > total:
            return "assigned NVIDIA GPU memory is invalid"
        if available < gpu_memory_bytes:
            return "assigned NVIDIA GPU has insufficient free memory"
    return ""


def cuda_visible_devices(assignment: dict) -> str:
    """Return the verified full UUID list suitable for CUDA visibility."""
    if not isinstance(assignment, dict):
        raise ValueError("gpu_assignment must be an object")
    uuids = assignment.get("uuids")
    count = len(uuids) if isinstance(uuids, list) else 0
    memory = assignment.get("gpu_memory_bytes")
    normalized = normalize_gpu_assignment(assignment, count, memory)
    return ",".join(normalized["uuids"])
