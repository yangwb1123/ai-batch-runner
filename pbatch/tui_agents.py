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
