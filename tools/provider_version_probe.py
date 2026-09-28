"""Private Linux subreaper for one public provider --version invocation.

The standalone process owns every adopted orphan. No prctl state is changed in
the caller, so unrelated caller subprocesses cannot be adopted or terminated.
"""

from __future__ import annotations

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from provider_smoke_support import OwnedProcesses, _wait_version
from tui_soak_support import SoakFailure, is_alive, process_identity


def enable_subreaper() -> None:
    if not sys.platform.startswith("linux"):
        raise SoakFailure("provider version ownership requires Linux")
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "cannot isolate provider version descendants")


def reap_adopted_children() -> bool:
    """Sweep after waitpid: even instantaneous double-fork children are ours."""
    deadline, found_live = time.monotonic() + 5, False
    while time.monotonic() < deadline:
        children = OwnedProcesses._child_pids(os.getpid())
        if not children:
            return found_live
        for pid in children:
            identity = process_identity(pid)
            if identity and is_alive(identity):
                found_live = True
                try:
                    OwnedProcesses(pid).cleanup()
                except SoakFailure:
                    if is_alive(identity):
                        raise
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        time.sleep(0.005)
    raise SoakFailure("provider version descendants did not stop within their deadline")


def inspect_version(provider: Path) -> str:
    enable_subreaper()
    error, process, owned = None, None, None
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen([str(provider), "--version"], stdout=output,
                                       stderr=output, start_new_session=True)
            owned = OwnedProcesses(process.pid)
            _wait_version(process, owned, output)
        except (OSError, SoakFailure, subprocess.TimeoutExpired) as exc:
            error = exc
        finally:
            if owned:
                owned.cleanup()
            if process:
                process.wait(timeout=5)
            orphaned = reap_adopted_children()
        if error:
            raise error
        if orphaned:
            raise SoakFailure("provider version command left live owned processes")
        output.seek(0)
        value = output.read(4097)
    if process.returncode or not value.strip() or len(value) > 4096:
        raise SoakFailure("provider version command failed or exceeded its output limit")
    return value.decode("utf-8", "replace").strip()


def main() -> int:
    try:
        version = inspect_version(Path(sys.argv[1]))
        result = {"version": version, "error": ""}
    except (OSError, SoakFailure, subprocess.TimeoutExpired) as exc:
        result = {"version": "", "error": str(exc)}
    print(json.dumps(result), flush=True)
    return 1 if result["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
