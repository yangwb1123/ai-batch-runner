"""Fact collection for campaign evaluation.

The evaluation core (``pbatch.campaign_evaluation``) is pure: it turns facts
into a scorecard. This module owns the only impure part - reading a campaign's
state rows, supervisor log, git history, worktree root, and disk usage - so the
core stays testable without a repository, a provider, or a clock.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .campaign_evaluation import (DATE_RE, PREFLIGHT_RE, PRESERVED_RE,
                                  QUOTA_RE, SEGMENT_RE, STALL_RE, STATUS_RE,
                                  SWEEP_RE, GitFacts, Scorecard,
                                  SupervisorFacts)
from .campaign_evaluation_dimensions import evaluate

# ── fact collection ──────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _diff_facts(repo: Path, rev: str, merge_marker: str) -> tuple[tuple[str, ...], list[str], int, int, int]:
    dates = tuple(line for line in _git(repo, "log", "--format=%ad", "--date=short",
                                        rev).splitlines() if DATE_RE.match(line))
    files = [line for line in _git(repo, "diff", "--name-only", rev).splitlines() if line]
    shortstat = _git(repo, "diff", "--shortstat", rev)
    insertions = _stat_count(shortstat, r"(\d+) insertions?\(\+\)")
    deletions = _stat_count(shortstat, r"(\d+) deletions?\(-\)")
    merges = _git(repo, "log", "--format=%s", rev).count(merge_marker)
    return dates, files, insertions, deletions, merges


def _stat_count(shortstat: str, pattern: str) -> int:
    match = re.search(pattern, shortstat)
    return int(match.group(1)) if match else 0


def _divergence_facts(repo: Path, upstream: str) -> tuple[int, int, float]:
    if not upstream:
        return 0, 0, 0.0
    ahead = int(_git(repo, "rev-list", "--count", f"{upstream}..HEAD").strip() or 0)
    behind = int(_git(repo, "rev-list", "--count", f"HEAD..{upstream}").strip() or 0)
    base = _git(repo, "merge-base", "HEAD", upstream).strip()
    stamp = _git(repo, "show", "-s", "--format=%ct", base).strip() if base else ""
    age = 0.0
    if stamp.isdigit():
        age = max(0.0, (datetime.now(timezone.utc).timestamp() - int(stamp)) / 86400.0)
    return ahead, behind, age


def _scope_facts(repo: Path, rev: str, files: list[str],
                 roots: tuple[str, ...],
                 framework_free_dirs: tuple[str, ...],
                 forbidden: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    outside = tuple(name for name in files
                    if roots and not any(name.startswith(root) for root in roots))
    violations: list[str] = []
    for directory in framework_free_dirs:
        violations.extend(
            _git(repo, "grep", "-lE", r"from '(vue|react|solid|svelte)'", "HEAD",
                 "--", directory).split())
    blocked = tuple(name for name in files
                    if any(name == item.rstrip("/") or name.startswith(item)
                           for item in forbidden))
    return outside[:10], tuple(violations[:5]), blocked[:10]


def collect_git_facts(repo: Path, baseline: str, *, upstream: str = "",
                      roots: tuple[str, ...] = (),
                      framework_free_dirs: tuple[str, ...] = ("packages/core/src",),
                      forbidden: tuple[str, ...] = (
                          "dist/", "node_modules/", "pnpm-lock.yaml",
                          "manifest.json", "llms.txt",
                          "scripts/arch-baseline.json", ".changeset/"),
                      merge_marker: str = "[pi-batch campaign] merge") -> GitFacts:
    """Read the campaign branch's real diff, divergence, and scope breaches."""
    rev = f"{baseline}..HEAD"
    dates, files, insertions, deletions, merges = _diff_facts(repo, rev, merge_marker)
    outside, violations, blocked = _scope_facts(
        repo, rev, files, roots, framework_free_dirs, forbidden)
    ahead, behind, age = _divergence_facts(repo, upstream)
    cross_surface = 0
    if roots:
        cross_surface = len([line for line in _git(repo, "log", "--format=%H", rev,
                                                  "--", "packages/icons").splitlines()
                             if line])
    return GitFacts(merges=merges, merge_dates=dates, files_changed=len(files),
                    insertions=insertions, deletions=deletions, ahead=ahead,
                    behind=behind, merge_base_age_days=age,
                    cross_surface_commits=cross_surface,
                    files_outside_roots=outside,
                    framework_free_violations=violations, forbidden_paths=blocked)




def collect_supervisor_facts(text: str) -> SupervisorFacts:
    segments = len(SEGMENT_RE.findall(text))
    stall = len(STALL_RE.findall(text))
    preflight = len(PREFLIGHT_RE.findall(text))
    quota = len(QUOTA_RE.findall(text))
    return SupervisorFacts(
        segments=segments, stall_breaks=stall, preflight_failures=preflight,
        quota_cooldowns=quota,
        sweeps_removed=len(SWEEP_RE.findall(text)),
        preserved_tracked=len(PRESERVED_RE.findall(text)),
        status_lines=len(STATUS_RE.findall(text)))


def read_rows(path: Path) -> list[dict]:
    """Read campaign state rows, skipping torn lines."""
    rows: list[dict] = []
    if not path or not Path(path).exists():
        return rows
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def scan_probe_files(root: Path) -> tuple[int, int]:
    """Return (probe files found, probe files that are tracked)."""
    if not root.is_dir():
        return 0, 0
    found = tracked = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        name = path.name
        if not (name.startswith("__scratch") or name.startswith("__probe")
                or name.startswith(".audit-")):
            continue
        if "node_modules" in path.parts or "dist" in path.parts:
            continue
        found += 1
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if subprocess.run(["git", "-C", str(root), "--literal-pathspecs",
                           "ls-files", "--error-unmatch", "--", relative],
                          capture_output=True, check=False).returncode == 0:
            tracked += 1
    return found, tracked


def evaluate_campaign(*, state_path: Path, repo: Path, baseline: str,
                      supervisor_log: Path | None = None,
                      upstream: str = "", roots: tuple[str, ...] = (),
                      window_hours: float = 0.0,
                      worktree_root: Path | None = None,
                      now: datetime | None = None) -> Scorecard:
    """Collect every fact a campaign window emits and score it."""
    rows = read_rows(Path(state_path))
    supervisor = collect_supervisor_facts(
        supervisor_log.read_text(encoding="utf-8", errors="replace")
        if supervisor_log and supervisor_log.exists() else "")
    git_facts = collect_git_facts(Path(repo), baseline, upstream=upstream,
                                  roots=roots)
    if not window_hours:
        window_hours = _window_hours(rows, now)
    probe_files = probe_tracked = 0
    worktree_count = 0
    disk_bytes = 0
    if worktree_root and Path(worktree_root).exists():
        probe_files, probe_tracked = scan_probe_files(Path(worktree_root))
        worktree_count = sum(1 for path in Path(worktree_root).glob("*/*")
                             if path.is_dir())
        disk_bytes = _tree_bytes(Path(worktree_root))
    return evaluate(rows, supervisor, git_facts, window_hours=window_hours,
                    probe_files=probe_files, probe_tracked=probe_tracked,
                    worktree_count=worktree_count, disk_bytes=disk_bytes,
                    upstream=upstream, now=now)


def _window_hours(rows: list[dict], now: datetime | None) -> float:
    """Window length from the state's own timestamps (no config needed)."""
    stamps = []
    for row in rows:
        try:
            stamps.append(datetime.fromisoformat(str(row.get("updated_at", ""))))
        except ValueError:
            continue
    if not stamps:
        return 0.0
    end = now or max(stamps)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    return max(0.0, (end - min(stamps)).total_seconds() / 3600.0)


def _tree_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_symlink():
                continue
            total += path.stat().st_size
        except OSError:
            continue
    return total
