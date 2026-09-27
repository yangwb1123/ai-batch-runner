"""Launch this application and its bundled checkers in source or frozen form."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from typing import Optional

SCRIPT_FLAG = "--_bundled-script"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def cli_argv(*args: str, entrypoint: Optional[Path] = None) -> list[str]:
    """Preserve a source checkout's entrypoint without passing it to an EXE."""
    if is_frozen():
        prefix = [sys.executable]
    elif entrypoint is not None and Path(entrypoint).is_file():
        prefix = [sys.executable, str(entrypoint)]
    else:
        prefix = [sys.executable, "-m", "pbatch"]
    return [*prefix, *map(str, args)]


def bundle_root() -> Path:
    """The executable's own assets; project configuration cannot override it."""
    extraction = getattr(sys, "_MEIPASS", "")
    if not is_frozen() or not extraction:
        raise ValueError("bundled scripts require the standalone executable")
    return (Path(extraction) / "share" / "ai-batch-runner").resolve()


def package_source_dir() -> Path:
    """Real Python files for AST checks, including the frozen source snapshot."""
    return bundle_root() / "pbatch" if is_frozen() else Path(__file__).resolve().parent


def _bundled_script(script: str) -> tuple[Path, str]:
    root = bundle_root()
    candidate = Path(script)
    path = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError("script is outside the executable's bundled assets") from exc
    allowed = relative == Path("quality.py") or (
        relative.parent == Path("scripts") and relative.suffix == ".py")
    if not allowed or not path.is_file():
        raise ValueError(f"not a bundled Python checker: {relative}")
    return path, relative.as_posix()


def script_argv(script: str, *args: str) -> list[str]:
    """Run a checker with embedded Python, preserving normal source behavior."""
    if not is_frozen():
        return [sys.executable, str(script), *map(str, args)]
    _, identifier = _bundled_script(str(script))
    return [sys.executable, SCRIPT_FLAG, identifier, *map(str, args)]


def dispatch_bundled_script(argv: list[str]) -> bool:
    """Handle the private child entrypoint before the public CLI parser."""
    if not argv or argv[0] != SCRIPT_FLAG:
        return False
    try:
        if len(argv) < 2:
            raise ValueError("missing bundled script identifier")
        script, _ = _bundled_script(argv[1])
    except ValueError as exc:
        print(f"pi-batch: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    old_argv, old_path = sys.argv[:], sys.path[:]
    try:
        sys.argv[:] = [str(script), *argv[2:]]
        sys.path.insert(0, str(script.parent))
        runpy.run_path(str(script), run_name="__main__")
    finally:
        sys.argv[:], sys.path[:] = old_argv, old_path
    return True
