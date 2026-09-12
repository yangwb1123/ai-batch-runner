"""Module discovery, analysis tasks, and direction selection."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .campaign_models import CampaignSettings, Direction
from .memory_io import reverse_lines
from .campaign_state import digest, tool_digest, tree_digest
from . import config
from .evolution import load_constitution
from .meta import _extract_json_array
from .models import Task, TaskResult
from .runner import run_parallel
from .triage import is_retryable

_PLANNING_FAILURE_STATUSES = frozenset({
    "FAILED", "VALIDATION_FAILED", "GATE_REJECTED", "GATE_REJECTED_OBSERVED",
    "PIPELINE_FAILED", "PROVIDER_BREAKER_PAUSED", "ANALYSIS_FAILED",
    "FAILED_OBSERVED",
})


def discover_modules(root: Path, settings: CampaignSettings, explicit: str = "") -> list[str]:
    """Discover bounded module directories, rejecting paths outside root."""
    if explicit:
        modules = [_safe_module(root, item.strip()) for item in explicit.split(",") if item.strip()]
        return sorted(dict.fromkeys(modules))
    modules = []
    for root_name in settings.roots:
        layer = _inside(root, root / root_name)
        if layer.is_dir():
            modules.extend(_walk_modules(root, layer, settings))
    if not modules and settings.fallback_top_level:
        modules = _fallback_modules(root, settings)
    return sorted(dict.fromkeys(modules))


def _safe_module(root: Path, value: str) -> str:
    candidate = root / value
    try:
        if candidate.is_symlink():
            raise ValueError(f"module is not a real directory: {value}")
        path = _inside(root, candidate)
        if not path.is_dir():
            raise ValueError(f"module is not a real directory: {value}")
    except OSError as exc:
        # robustness: paths longer than the FS limit (or otherwise
        # unstat-able) must fail as invalid modules, not crash the
        # whole campaign (live finding: File name too long)
        raise ValueError(f"module not accessible: {value} ({exc})") from exc
    return path.relative_to(root).as_posix()


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes repository: {path}") from exc
    return resolved


def _walk_modules(root: Path, layer: Path, settings: CampaignSettings) -> list[str]:
    modules = []
    for path in sorted(layer.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_dir() or path.is_symlink() or _excluded(relative, settings.excludes):
            continue
        depth = len(path.relative_to(layer).parts)
        if depth <= settings.max_depth and _contains_files(path):
            modules.append(path.relative_to(root).as_posix())
    if not modules and _contains_files(layer):
        modules.append(layer.relative_to(root).as_posix())
    return modules


def _fallback_modules(root: Path, settings: CampaignSettings) -> list[str]:
    return [path.relative_to(root).as_posix() for path in sorted(root.iterdir())
            if path.is_dir() and not path.is_symlink() and
            not _excluded(path.relative_to(root), settings.excludes) and _contains_files(path)]


# 打包/缓存产物目录：永远不是分析目标（campaign 实战：ai_batch_runner.egg-info
# 被当作模块分析浪费一轮流水线；其方向多是打包/版本噪音）。
_PACKAGE_ARTIFACT_DIRS = ("egg-info", "__pycache__", "dist", "build",
                          "node_modules", ".venv", "venv")


def _excluded(path: Path, excludes: tuple[str, ...]) -> bool:
    lowered = {item.lower() for item in path.parts}
    hidden = any(item.startswith(".") for item in path.parts)
    artifact = any(term in item or item.endswith(term)
                   for item in lowered for term in _PACKAGE_ARTIFACT_DIRS)
    return hidden or artifact or any(term.lower() in lowered
                                     for term in excludes)


def _contains_files(path: Path) -> bool:
    try:
        return any(item.is_file() and not item.name.startswith(".") for item in path.iterdir())
    except OSError:
        return False


def module_slug(module: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "-", module).strip("-").lower() or "module"
    return f"{base[:56]}-{digest(module)[:8]}"


def analysis_prompt(settings: CampaignSettings, module: str) -> str:
    return (settings.analysis_prompt
            .replace("{module}", module)
            .replace("{module_path}", module + "/")
            .replace("{candidate_limit}", str(settings.candidate_limit)))


# L6/L10 注入上限：已拒方向最多带最近 N 条（有界，防 prompt 膨胀）。
_MAX_REJECTED_INJECT = 12
_STATE_LINE_SCAN = 2000


def _evolution_context(root: Path, state_path: Path | None = None) -> str:
    """项目宪法 + 历史已拒方向 → 分析 prompt 附加块（有界）。

    L6（明确不做）与 L10（决策记忆）的 campaign 挂点：方向建议必须
    知道项目边界与历史被拒记录——snaplink state.jsonl 里大量
    `DIRECTION_REJECTED: duplicate` 证明不带历史的分析会重复建议。
    """
    parts = []
    constitution = load_constitution(root)
    goals = constitution.get("explicit_non_goals") or []
    if isinstance(goals, list) and goals:
        goals_text = "\n".join(f"- {g}" for g in goals)
        parts.append("Project constitution explicit non-goals "
                     "(MUST NOT propose directions that collide):\n" + goals_text)
    budget = (constitution.get("change_budget") or {})
    if budget.get("max_files"):
        parts.append(f"Change budget: max_files={budget['max_files']}, "
                     f"max_lines={budget.get('max_lines', 0)}")
    if state_path is not None:
        states = [state_path]
    else:
        states = [config.data_dir(str(root)) / "state.jsonl",
                  root / "docs" / "auto" / "state.jsonl"]
    rejected = []
    seen = set()
    for state in states:
        if not state.is_file():
            continue
        for item in _recent_rejected(state, limit=_MAX_REJECTED_INJECT):
            key = (item[0], item[1])
            if key not in seen:
                seen.add(key)
                rejected.append(item)
            if len(rejected) >= _MAX_REJECTED_INJECT:
                break
        if len(rejected) >= _MAX_REJECTED_INJECT:
            break
    if rejected:
        lines = "\n".join(f"- {d[:120]} ({r})" for d, r in rejected)
        parts.append("Previously rejected directions (do NOT re-propose "
                     "the same or overlapping work):\n" + lines)
    return "\n\n".join(parts)


def _recent_rejected(state: Path, limit: int = _MAX_REJECTED_INJECT) -> list:
    """从 state.jsonl 尾部扫描最近被拒方向（有界行缓冲，倒序收集）。"""
    rejected = []
    try:
        tail = []
        for line in reverse_lines(state, maximum=256 * 1024):
            if line is None:
                continue
            tail.append(line)
            if len(tail) >= _STATE_LINE_SCAN:
                break
    except (OSError, ValueError):
        return []
    for line in tail:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        status = row.get("status")
        direction = row.get("direction")
        if status in ("DIRECTION_REJECTED", "GATE_REJECTED") and direction:
            rejected.append((str(direction), str(row.get("reason") or "")))
        if len(rejected) >= limit:
            break
    return rejected


def build_analysis_task(root: Path, settings: CampaignSettings, module: str,
                        model: str = "", provider: str = "", timeout: int = 0,
                        focus: str = "") -> Task:
    output = settings.output_path(root) / "analyses" / f"{module_slug(module)}.json"
    prompt = analysis_prompt(settings, module)
    if str(focus or "").strip():
        prompt += ("\n\n==== Campaign focus request ====\n"
                   + str(focus).strip()[:2000]
                   + "\nPrioritize evidence relevant to this request; do not invent "
                     "module relevance.")
    context = _evolution_context(root, settings.state_path(root))
    if context:
        prompt += ("\n\n==== Project context (constitution + rejected history) ====\n"
                   + context)
    task = Task(prompt=prompt, output=str(output), cwd=str(root))
    task.memory = {"stage": "campaign-analysis", "module": module,
                   "memory_mode": "execute"}
    task.model = model or task.model
    task.provider = provider or task.provider
    if timeout:
        task.timeout = timeout
    return task


def analysis_fingerprint(root: Path, settings: CampaignSettings, module: str,
                         snapshot: dict, task: Task) -> str:
    return digest({
        "version": 2,
        "module": module,
        "module_tree": tree_digest(root / module, settings.excludes),
        # Scope reuse to this module and shared contract roots. A full
        # repository snapshot invalidated every module after any implementation.
        "repository_scope": _analysis_repository_scope(root, module, settings),
        "prompt": task.prompt,
        "model": task.model,
        "provider": task.provider,
        "thinking": config.AGENT_THINKING_OVERRIDE or task.thinking,
        "planning_history": _planning_history(root, module),
        "tool": tool_digest(),
    })


def _analysis_repository_scope(root: Path, module: str,
                               settings: CampaignSettings) -> dict:
    scope = {"module": module}
    for name in ("interfaces", "contracts", "schemas"):
        candidate = root / name
        if candidate.is_dir() and name != module:
            scope[name] = tree_digest(candidate, settings.excludes)
    return scope


def _planning_history(root: Path, module: str = "") -> list[dict]:
    """Return failures for this module, not unrelated campaign work."""
    from .memory import memory_enabled, recent
    if not memory_enabled():
        return []
    try:
        rows = recent(str(root), limit=48)
    except (OSError, ValueError):
        return []
    return [row for row in rows
            if row.get("status") in _PLANNING_FAILURE_STATUSES
            and (not module or str(row.get("module", "")) == module)]


def run_analysis_tasks(tasks: list[Task], jobs: int, retries: int,
                       retry_delay: float = 10, validator=None) -> dict[str, TaskResult]:
    """Run independent analyses in parallel and retry only failed tasks."""
    pending = list(tasks)
    final = {}
    for attempt in range(retries + 1):
        if not pending:
            break
        results = run_parallel(pending, jobs)
        for result in results:
            if result.success and validator and not validator(result):
                result.success = False
                result.reason = "analysis produced no structured directions"
                result.task.output_path().unlink(missing_ok=True)
            final[str(result.task.output_path())] = result
        pending = [result.task for result in results
                   if not result.success and is_retryable(result.reason)]
        if pending and attempt < retries:
            time.sleep(max(0, retry_delay) * (2 ** attempt))
    return final


def parse_directions(module: str, text: str) -> list[Direction]:
    """Parse structured JSON; Markdown headings remain report-only legacy items."""
    raw = _extract_json_array(text)
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = []
        if isinstance(data, list):
            directions = [Direction.from_mapping(module, item) for item in data if isinstance(item, dict)]
            return [item for item in directions if item.title]
    headings = re.findall(r"^##\s+(.+?)\s*$", text or "", re.MULTILINE)
    return [Direction(module=module, title=title.strip(), raw={"legacy_markdown": True})
            for title in headings if title.strip()]


def evidence_exists(root: Path, direction: Direction) -> bool:
    """True when any evidence path exists under the repo root.

    Real-world lesson (forge-os campaign): the analysis model cited evidence
    WITHOUT the module prefix — `internal/statefs/statefs.go:276` for a
    repository where the file lives at `forge-core/internal/statefs/statefs.go`.
    Every candidate is therefore probed bare first (models that DO include the
    prefix keep working), then with the direction's module prefixed. `_inside`
    rejects anything escaping the root either way.
    """
    module = direction.module
    for evidence in direction.evidence:
        for candidate in _evidence_candidates(evidence):
            if not candidate:
                continue
            for probe in (candidate, f"{module}/{candidate}"):
                try:
                    path = _inside(root, root / probe)
                except ValueError:
                    continue
                try:
                    if path.exists():
                        return True
                except OSError:
                    # untrusted evidence paths may exceed the FS limit
                    # (live finding: File name too long) — skip, never crash
                    continue
    return False


_EVIDENCE_TOKEN = re.compile(r"[A-Za-z0-9_./-]+")
_EVIDENCE_EXT = (".go", ".py", ".js", ".ts", ".tsx", ".md", ".yaml", ".yml",
                 ".json", ".mod", ".sum", ".toml", ".sh", ".sql", ".html", ".css")


def _evidence_candidates(evidence: str) -> list[str]:
    """Path-like candidates from one evidence string.

    Real-world lesson (aero-vault campaign): LLMs mix formats —
    "path/file.go:12 description", "path/file.go returns X on missing"
    (no colon) and prose without any path. Extract path tokens first
    (colon/file:line suffixes stripped), then fall back to the legacy
    whole-string prefix so old behaviour is preserved.
    """
    candidates: list[str] = []
    for token in _EVIDENCE_TOKEN.findall(evidence):
        token = re.sub(r":\d+(?::\d+)?$", "", token)
        token = token.strip("./")
        if not token or token in (".", ".."):
            continue
        if "/" in token or token.endswith(_EVIDENCE_EXT):
            candidates.append(token)
    prefix = evidence.split("#", 1)[0].strip()
    if prefix and prefix not in candidates:
        candidates.append(prefix)
    return candidates


def non_goal_collision(root: Path, direction: Direction) -> str:
    """方向撞项目明确不做（L6）→ 返回碰撞理由（空 = 通过）。

    读取项目宪法 explicit_non_goals（docs/evolution/constitution.yaml
    或 evolution.yaml）；无宪法时不拦截（vacuous-green ban：缺省边界
    不是通过信号，但也不臆造边界）。实战出处：iris-ui ROADMAP 明确
    不做清单阻止 agent 自动开工维护者未选择的方向。
    """
    from .evolution import check_conformance, load_constitution
    constitution = load_constitution(root)
    goals = constitution.get("explicit_non_goals") or []
    if not isinstance(goals, list) or not goals:
        return ""
    text = f"{direction.title} {direction.problem}"
    collisions = check_conformance(text, goals)
    if collisions:
        return f"non-goal collision: {collisions[0]['goal']}"
    return ""


def rejection_reason(root: Path, direction: Direction, minimum_score: float,
                     seen: list[Direction]) -> str:
    """Precise reason a direction was not selected (real-world lesson:
    a conflated "duplicate, low score, missing evidence..." string made
    a 9-direction rejection impossible to diagnose)."""
    collision = non_goal_collision(root, direction)
    if collision:
        return collision
    if any(_similar_direction(direction, previous) for previous in seen):
        return ("duplicate of a higher-ranked direction "
                "(title overlap >= 0.75 or evidence overlap >= 0.8)")
    if direction.score < minimum_score:
        return f"score {direction.score:.1f} below minimum {minimum_score:g}"
    missing = [name for name, value in (
        ("title", direction.title), ("problem", direction.problem),
        ("evidence", direction.evidence), ("acceptance", direction.acceptance))
        if not value]
    if missing:
        return "missing required field(s): " + ", ".join(missing)
    if not evidence_exists(root, direction):
        shown = " | ".join(direction.evidence[:2])
        return f"no evidence path exists in the repository (evidence: {shown})"
    return "rejected because the selection quota was already filled"


def select_directions(root: Path, directions: list[Direction], maximum: int,
                      minimum_score: float) -> tuple[list[Direction], list[tuple[Direction, str]]]:
    """Dedupe, score, and reject candidates without implementable evidence.
    Rejected items carry their precise reason (see rejection_reason)."""
    ordered = sorted(enumerate(directions), key=lambda item: (-item[1].score, item[0]))
    selected = []
    rejected = []
    seen: list[Direction] = []
    for _, direction in ordered:
        duplicate = any(_similar_direction(direction, previous) for previous in seen)
        blocked = bool(non_goal_collision(root, direction))
        valid = (not duplicate and not blocked and
                 direction.score >= minimum_score and
                 direction.implementable and evidence_exists(root, direction))
        seen.append(direction)
        if valid and len(selected) < maximum:
            selected.append(direction)
        else:
            rejected.append((direction, rejection_reason(root, direction, minimum_score, seen[:-1])))
    return selected, rejected


def _similar_direction(left: Direction, right: Direction) -> bool:
    left_words = set(re.findall(r"[\w\u3400-\u9fff]+", left.title.casefold()))
    right_words = set(re.findall(r"[\w\u3400-\u9fff]+", right.title.casefold()))
    union = left_words | right_words
    title_overlap = len(left_words & right_words) / len(union) if union else 1
    left_evidence = set(left.evidence)
    right_evidence = set(right.evidence)
    evidence_union = left_evidence | right_evidence
    evidence_overlap = (len(left_evidence & right_evidence) / len(evidence_union)
                        if evidence_union else 0)
    return title_overlap >= 0.75 or (title_overlap >= 0.4 and evidence_overlap >= 0.8)
