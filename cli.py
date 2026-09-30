#!/usr/bin/env python3
"""cli.py — engineering gate entry point (modeled on snaplink's cli.py).

Usage:
  python cli.py check            Filesize + complexity + root policy + style
  python cli.py check-filesize   File size budget
  python cli.py check-complexity Cyclomatic / cognitive budgets
  python cli.py check-root       Root directory policy
  python cli.py check-style      Whitespace hygiene
  python cli.py check --json     Machine-readable diagnostics
  python cli.py check-test       Run the harness's own unit tests
  python cli.py harness          Same as check (pre-push hook target)
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path


def run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    """Run a subprocess with live output (snaplink cli.py style)."""
    return subprocess.run(list(args), capture_output=False, text=True, check=False, **kwargs)


def _aggregate(checks, json_mode: bool = False) -> int:
    """Run checks, sum failure counts, clamp to exit code."""
    total = 0
    reports = {}
    for name, fn in checks:
        output = io.StringIO()
        try:
            if json_mode:
                with contextlib.redirect_stdout(output):
                    failures = int(fn() or 0)
            else:
                print(f"-- {name} --")
                failures = int(fn() or 0)
            total += failures
        except KeyboardInterrupt:
            raise
        except Exception as e:  # a broken gate fails the run, never crashes it
            failures = 1
            total += 1
            output.write(f"  ERROR: {name} crashed: {e}\n")
            if not json_mode:
                print(f"  ERROR: {name} crashed: {e}")
        if json_mode:
            reports[name] = [line for line in output.getvalue().splitlines() if line.strip()]
    if json_mode:
        reports["_summary"] = {"failed": total}
        print(json.dumps(reports, ensure_ascii=False, sort_keys=True))
    return 1 if total > 0 else 0


def cmd_check(json_mode: bool = False) -> int:
    return _aggregate([
        ("filesize", _check_filesize),
        ("complexity", _check_complexity),
        ("root-policy", _check_root),
        ("style", _check_style),
        ("project-gates", _check_project_gates),
    ], json_mode)


def _check_project_gates() -> int:
    from checks.project_gates import run as gates_run
    return gates_run()


def _check_filesize() -> int:
    from checks.filesize import run as fs_run
    return fs_run()


def _check_complexity() -> int:
    from checks.complexity import run as cx_run
    return cx_run()


def _check_root() -> int:
    from checks.root_files import run as rp_run
    return rp_run()


def _check_style() -> int:
    from checks.style import run as st_run
    return st_run()


def cmd_check_test() -> int:
    """Harness integrity: the gates must verify themselves (zero-dep unittest)."""
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "checks", "-p", "test_*.py"]
    return run(*cmd).returncode


def cmd_harness(json_mode: bool = False) -> int:
    # Keep the historical zero-argument delegation path intact for callers
    # that monkeypatch/provide a legacy ``cmd_check`` callable.
    return cmd_check(True) if json_mode else cmd_check()


def _call_mode(fn, json_mode: bool) -> int:
    """Call a gate without changing the legacy no-argument call contract."""
    return fn(True) if json_mode else fn()


ROOT = Path(__file__).resolve().parent


def _project_gate_command(name: str) -> str:
    """The gate a project declared, read from its own pi-batch.yaml."""
    import yaml
    config = ROOT / "projects" / name / "pi-batch.yaml"
    if not config.exists():
        raise SystemExit(f"no project definition: projects/{name}/pi-batch.yaml")
    document = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    entry = (document.get("validators") or {}).get("repo-gate") or {}
    command = entry.get("cmd") if isinstance(entry, dict) else None
    if not command:
        raise SystemExit(f"projects/{name}/pi-batch.yaml declares no repo-gate validator")
    return command


def cmd_project_gate() -> int:
    """List each target's declared gate, or run one of them in its repository.

    The declaration is what a campaign's implement stage validates on (see
    checks/project_gates.py), so running it here runs exactly the gate that
    decides whether batch output is accepted — in the target repository, with
    the target's own tooling.
    """
    projects = sorted(p for p in (ROOT / "projects").iterdir()
                      if p.is_dir() and (p / "pi-batch.yaml").exists())
    if len(sys.argv) < 3:
        print(f"{'project':22s} {'gate':52s} repository")
        for project in projects:
            try:
                gate = _project_gate_command(project.name)
            except SystemExit as error:
                gate = f"({error})"
            import yaml
            document = yaml.safe_load((project / "pi-batch.yaml").read_text(encoding="utf-8")) or {}
            declared = (document.get("project") or {}).get("target_repo", "")
            print(f"{project.name:22s} {gate:52s} {declared}")
        print("\nrun one with: python3 cli.py project-gate <project>")
        return 0
    name = sys.argv[2]
    command = _project_gate_command(name)
    import yaml
    document = yaml.safe_load((ROOT / "projects" / name / "pi-batch.yaml").read_text(encoding="utf-8")) or {}
    declared = (document.get("project") or {}).get("target_repo", "")
    repo = Path(declared).expanduser() if declared else None
    if repo is None or not repo.is_dir():
        print(f"FAIL: {name} declares no reachable target repository", file=sys.stderr)
        return 1
    print(f"$ cd {repo} && {command}", flush=True)
    return run("bash", "-lc", command, cwd=repo)


def main() -> int:
    json_mode = "--json" in sys.argv[2:]
    dispatch = {
        "check": lambda: _call_mode(cmd_check, json_mode),
        "check-filesize": lambda: _call_mode(
            lambda mode=False: _aggregate([("filesize", _check_filesize)], mode),
            json_mode),
        "check-complexity": lambda: _call_mode(
            lambda mode=False: _aggregate([("complexity", _check_complexity)], mode),
            json_mode),
        "check-root": lambda: _call_mode(
            lambda mode=False: _aggregate([("root-policy", _check_root)], mode),
            json_mode),
        "check-style": lambda: _call_mode(
            lambda mode=False: _aggregate([("style", _check_style)], mode),
            json_mode),
        "check-test": cmd_check_test,
        "project-gate": cmd_project_gate,
        "harness": lambda: _call_mode(cmd_harness, json_mode),
    }
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    fn = dispatch.get(sys.argv[1])
    if fn is None:
        print(f"unknown command: {sys.argv[1]}", file=sys.stderr)
        print(__doc__.strip(), file=sys.stderr)
        return 2
    return fn()


if __name__ == "__main__":
    sys.exit(main())
