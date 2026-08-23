"""Dependency-free projection of the shared agent run ledger."""

from __future__ import annotations

from .agent_ledger import query


def _truncate(value: object, width: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"[:width]
    return text[:width - 1] + "…"


class AgentLedgerView:
    """Read and render the newest run observed for at most eight agents."""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def read(self, data_dir, limit: int = 30) -> list[dict]:
        self.entries = query(data_dir, limit=limit)
        return list(self.entries)

    def _latest_by_agent(self) -> list[dict]:
        latest = {}
        for entry in reversed(self.entries):
            name = str(entry.get("agent") or "unknown")
            if name not in latest:
                latest[name] = entry
                if len(latest) == 8:
                    break
        return [latest[name] for name in sorted(latest)]

    def render_lines(self, width: int) -> list[str]:
        """Render one bounded line per agent, or an explicit empty state."""
        width = max(1, int(width))
        entries = self._latest_by_agent()
        if not entries:
            return [_truncate("No agent runs recorded.", width)]
        lines = []
        for entry in entries:
            timestamp = str(entry.get("ts") or "-").replace("T", " ").removesuffix("Z")
            line = (f"{entry.get('agent') or 'unknown'} | "
                    f"{entry.get('status') or '-'} | {timestamp} | "
                    f"{entry.get('task_brief') or '-'}")
            lines.append(_truncate(line, width))
        return lines


def render_agent_lines(data_dir, width: int, limit: int = 30) -> list[str]:
    """Read and render the ledger in one call for the thin TUI mount."""
    view = AgentLedgerView()
    view.read(data_dir, limit)
    return view.render_lines(width)


def render_breaker_lines(data_dir, width: int = 80) -> list[str]:
    """Project the shared provider breaker without exposing mutable state."""
    width = max(1, int(width))
    try:
        from .provider_breaker import breaker_state
        state = breaker_state(data_dir)
        status = (f"ACTIVE · {state['cooldown_remaining_s']}s remaining · "
                  f"step {state['step']}" if state["active"] else "ready")
        lines = ["PROVIDER BREAKER", f"state     {status}",
                 f"streak    {state['streak']}/{state['threshold']}",
                 f"shared    {state['shared_state_file']}"]
    except Exception as exc:
        lines = ["PROVIDER BREAKER", f"state     unavailable: {exc}"]
    return [_truncate(line, width) for line in lines]


class RunsView:
    """最近 run 明细投影（跨进程共享账本）：agent/状态/重试/任务摘要。

    /runs 面板的数据源与 headless `pi-batch ops runs` 相同——TUI 与
    CLI、其他实例看到同一份任务历史（自我修复/重试过程可见）。
    """

    MAX_LINES = 24

    def __init__(self, limit: int = 20) -> None:
        self.limit = limit

    def read(self, data_dir: str) -> list[dict]:
        from .agent_ledger import query
        rows = query(data_dir, limit=self.limit)
        return [{
            "time": str(r.get("ts", ""))[11:19],
            "agent": str(r.get("agent", ""))[:10],
            "status": str(r.get("status", ""))[:10],
            "attempt": int(r.get("attempt", 0) or 0),
            "task": _truncate(r.get("task_brief", ""), 40),
        } for r in rows]

    def render_lines(self, data_dir: str, width: int = 100) -> list[str]:
        rows = self.read(data_dir)
        if not rows:
            return ["Runs: no runs yet (账本为空)"]
        lines = ["RUNS (shared ledger, latest first)"]
        for row in rows[: self.MAX_LINES]:
            marker = {"succeeded": "✓", "failed": "✗", "rejected": "!", 
                      "cancelled": "-"}.get(row["status"], "?")
            task = _truncate(row["task"], max(10, width - 46))
            lines.append(
                f"{row['time']} {marker} {row['agent']:<10} "
                f"{row['status']:<10} #{row['attempt']} {task}"[:width])
        return lines



def _clean_text(value) -> str:
    import re
    return re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))",
                  "", str(value or ""))


def _inline_label(value) -> str:
    label = " ".join(str(value or "").split())
    return label if label else "<none>"


def render_workspace_files(payload: dict) -> str:
    error = _clean_text(payload.get("error", ""))
    if error:
        return f"## Workspace files\n\nUnable to inspect workspace: {error}"
    if not payload.get("available"):
        return "## Workspace files\n\nGit workspace information is unavailable."
    files = payload.get("files", [])
    lines = [
        "## Workspace files", "",
        f"Branch: `{_inline_label(payload.get('branch') or 'unknown')}` · "
        f"Changed: **{payload.get('changed', len(files))}** · "
        f"Staged: {payload.get('staged', 0)} · "
        f"Unstaged: {payload.get('unstaged', 0)} · "
        f"Untracked: {payload.get('untracked', 0)}",
    ]
    if not files:
        lines.extend(["", "Working tree is clean."])
        return "\n".join(lines)
    lines.extend(["", "| Status | Path |", "|:--:|---|"])
    for item in files:
        path = _inline_label(item.get("path", "")).replace("|", "\\|")
        lines.append(f"| `{item.get('status', '')}` | `{path}` |")
    if payload.get("truncated"):
        lines.extend(["", "_File list truncated at the display limit._"])
    return "\n".join(lines)


def render_workspace_diff(payload: dict) -> str:
    error = _clean_text(payload.get("error", ""))
    label = _inline_label(payload.get("path", "")) or "workspace"
    if error:
        return f"## Diff · `{label}`\n\nUnable to generate diff: {error}"
    patch = _clean_text(payload.get("patch", ""))
    fence = "```"
    while fence in patch:
        fence += "`"
    note = ("\n\n_Diff truncated at the bounded output limit._"
            if payload.get("truncated") else "")
    return f"## Diff · `{label}`\n\n{fence}diff\n{patch}\n{fence}{note}"


