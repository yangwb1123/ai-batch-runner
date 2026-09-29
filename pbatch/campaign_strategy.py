"""Deterministic campaign planning from focus text and token telemetry."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
from typing import Iterable


_MAX_LINES = 8192
_MAX_LINE_BYTES = 256 * 1024
_MAX_USAGE_BYTES = 8 * 1024 * 1024
_MAX_SIGNAL_FILES = 32
_MAX_SIGNAL_ENTRIES_PER_DIR = 256
_MAX_SIGNAL_BYTES = 4096
_TOKEN_RE = re.compile(
    r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+|[\u4e00-\u9fff]+",
    re.UNICODE)
_TOKEN_FIELDS = {
    "input_tokens": "input_tokens", "inputTokens": "input_tokens",
    "prompt_tokens": "input_tokens", "input": "input_tokens",
    "output_tokens": "output_tokens", "outputTokens": "output_tokens",
    "completion_tokens": "output_tokens", "output": "output_tokens",
    "total_tokens": "total_tokens", "totalTokens": "total_tokens",
    "total": "total_tokens", "cacheRead": "cache_tokens",
    "cache_read_tokens": "cache_tokens", "cost_total": "cost_usd",
}


@dataclass(frozen=True)
class UsageSummary:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    truncated: bool = False
    calls_with_usage: int = 0

    @property
    def token_telemetry(self) -> str:
        if not self.calls:
            return "no_calls"
        observed = (self.calls_with_usage or self.total_tokens > 0
                    or self.input_tokens > 0 or self.output_tokens > 0)
        if not observed:
            return "unknown"
        return "partial" if self.calls_with_usage and self.calls_with_usage < self.calls else "observed"

    @property
    def observed_average(self) -> int:
        return self.total_tokens // self.calls if self.calls else 0

    def diagnostics(self, estimate: int) -> tuple[str, ...]:
        if not self.calls:
            return ()
        warnings = []
        if self.truncated:
            warnings.append("telemetry ledger is bounded; use an isolated events file")
        if estimate > 0 and self.observed_average > estimate * 1.5:
            warnings.append("average analysis tokens exceed estimate by >50%")
        if self.input_tokens > max(1, self.output_tokens) * 4:
            warnings.append("input-heavy context; trim injected evidence")
        if self.output_tokens > max(1, self.input_tokens) * 2:
            warnings.append("output-heavy response; reduce candidate/detail limits")
        if self.cost_usd > 0 and self.cost_usd / self.calls > 0.10:
            warnings.append("average analysis cost exceeds $0.10/call")
        return tuple(warnings)


@dataclass(frozen=True)
class CampaignPlan:
    selected: tuple[str, ...]
    skipped: tuple[str, ...] = ()
    focus: str = ""
    usage: UsageSummary = UsageSummary()
    estimated_tokens: int = 0
    reason: str = ""
    blocked: bool = False
    baseline_tokens: int = 0

    def mapping(self) -> dict:
        return {
            "selected": list(self.selected), "skipped": list(self.skipped),
            "focus": self.focus[:512], "usage": {
                "input_tokens": self.usage.input_tokens,
                "output_tokens": self.usage.output_tokens,
                "total_tokens": self.usage.total_tokens,
                "cache_tokens": self.usage.cache_tokens,
                "calls": self.usage.calls,
                "calls_with_usage": self.usage.calls_with_usage,
                "token_telemetry": self.usage.token_telemetry,
                "cost_usd": self.usage.cost_usd,
                "truncated": self.usage.truncated,
                "diagnostics": list(self.usage.diagnostics(
                    self.baseline_tokens or self.estimated_tokens)),
            }, "estimated_tokens": self.estimated_tokens,
            "baseline_tokens": self.baseline_tokens,
            "reason": self.reason[:512], "blocked": self.blocked,
        }


def no_pending_plan(modules: Iterable[str], *, focus: str = "",
                    usage: UsageSummary = UsageSummary(),
                    token_estimate: int = 8_000,
                    baseline_tokens: int = 0) -> CampaignPlan:
    baseline = baseline_tokens or token_estimate
    return CampaignPlan((), tuple(sorted(set(str(item) for item in modules))),
                        str(focus or "").strip(), usage, baseline,
                        "no pending analysis (reuse)", False, baseline)


def plan_modules(modules: Iterable[str], *, focus: str = "",
                 batch_size: int = 0, token_budget: int = 0,
                 usage: UsageSummary = UsageSummary(),
                 token_estimate: int = 8_000,
                 priority: dict[str, int] | None = None,
                 signals: dict[str, str] | None = None,
                 max_calls: int | None = None,
                 baseline_tokens: int = 0) -> CampaignPlan:
    """Select a bounded, deterministic module batch without LLM calls."""
    _validate_limits(batch_size, token_budget, token_estimate)
    baseline = max(1, baseline_tokens or token_estimate)
    values = tuple(sorted(dict.fromkeys(str(item) for item in modules if str(item))))
    call_plan = _call_limit_plan(values, focus, usage, baseline, max_calls)
    if call_plan is not None:
        return call_plan
    focus_text = str(focus or "").strip()
    hints = signals or {}
    ranked = _rank(values, focus_text, priority or {}, hints)
    matched = _focus_matches(values, focus_text, hints)
    if focus_text and not matched:
        return CampaignPlan((), values, focus_text, usage, 0,
                            "focus did not match discovered modules", True,
                            baseline)
    if focus_text:
        ranked = [item for item in ranked if item[0] in matched]
    effective_batch = _effective_batch(batch_size, max_calls)
    ordered = _bounded_order(ranked, effective_batch)
    budget_error = _token_budget_error(token_budget, usage)
    if budget_error:
        return CampaignPlan((), tuple(ordered), focus_text, usage, baseline,
                            budget_error, True, baseline)
    estimate = max(1, usage.observed_average or baseline)
    ordered, blocked = _budget_order(ordered, token_budget, usage.total_tokens, estimate)
    if blocked:
        return CampaignPlan((), tuple(ordered), focus_text, usage, estimate,
                            "analysis token budget exhausted", True, baseline)
    chosen = tuple(ordered)
    skipped = tuple(module for module in values if module not in chosen)
    return CampaignPlan(chosen, skipped, focus_text, usage, estimate,
                        _plan_reason(focus_text, effective_batch, token_budget,
                                     max_calls, batch_size), False,
                        baseline)


def _token_budget_error(token_budget: int, usage: UsageSummary) -> str:
    if not token_budget:
        return ""
    if usage.truncated:
        return "token telemetry is truncated; use a dedicated events file"
    if usage.calls and usage.token_telemetry != "observed":
        return "token telemetry is unknown or partial; cannot enforce token budget"
    return ""


def _call_limit_plan(values: tuple[str, ...], focus: str,
                     usage: UsageSummary, estimate: int,
                     max_calls: int | None) -> CampaignPlan | None:
    if max_calls is not None and max_calls < 0:
        raise ValueError("campaign invocation allowance must be non-negative")
    if max_calls == 0 and values:
        return CampaignPlan((), values, str(focus or "").strip(), usage, estimate,
                            "provider invocation allowance exhausted", True,
                            estimate)
    return None


def _focus_matches(values: tuple[str, ...], focus: str,
                   hints: dict[str, str]) -> set[str]:
    wanted = _tokens(focus)
    return {module for module in values if wanted &
            _tokens(hints.get(module, module))}


def _effective_batch(batch_size: int, max_calls: int | None) -> int:
    if max_calls is None:
        return batch_size
    return min(batch_size, max_calls) if batch_size else max_calls


def _validate_limits(batch_size: int, token_budget: int, estimate: int) -> None:
    if batch_size < 0 or token_budget < 0 or estimate <= 0:
        raise ValueError("campaign strategy limits must be non-negative")


def _bounded_order(ranked: list[tuple[str, int]], batch_size: int) -> list[str]:
    ordered = [module for module, _ in ranked]
    return ordered[:batch_size] if batch_size else ordered


def _budget_order(ordered: list[str], budget: int, spent: int,
                  estimate: int) -> tuple[list[str], bool]:
    if not budget:
        return ordered, False
    remaining = max(0, budget - spent)
    if remaining < estimate:
        return ordered, True
    return ordered[:max(1, remaining // estimate)], False


def _plan_reason(focus: str, batch_size: int, budget: int,
                 max_calls: int | None = None, configured_batch: int = 0) -> str:
    if max_calls is not None and (not configured_batch or max_calls < configured_batch):
        return "provider-budget bounded"
    if focus:
        return "focus-ranked"
    if batch_size or budget:
        return "bounded-batch"
    return "all-modules (no strategy limits configured)"


def read_usage(path: str, *, run_id: str = "") -> UsageSummary:
    """Read bounded token telemetry for one campaign run."""
    rows, truncated = _read_rows(path)
    total: dict[str, float] = {}
    calls = 0
    calls_with_usage = 0
    for row in rows:
        if not _is_analysis_event(row, run_id):
            continue
        calls += 1
        usage = row.get("usage")
        if isinstance(usage, dict):
            if any(_TOKEN_FIELDS.get(str(key)) in {
                    "input_tokens", "output_tokens", "total_tokens", "cache_tokens"}
                   for key in usage):
                calls_with_usage += 1
            _add_usage(total, usage)
    observed = total.get("total_tokens", 0)
    if not observed:
        observed = total.get("input_tokens", 0) + total.get("output_tokens", 0)
    cost = total.get("cost_usd", 0.0) or _cost_from_rows(rows, run_id)
    return UsageSummary(total.get("input_tokens", 0), total.get("output_tokens", 0),
                        observed, total.get("cache_tokens", 0), calls,
                        float(cost), truncated, calls_with_usage)


def _read_rows(path: str) -> tuple[list[dict], bool]:
    if not path:
        return [], False
    try:
        file_path = Path(path)
        size = file_path.stat().st_size
        truncated = size > _MAX_USAGE_BYTES
        with file_path.open("rb") as handle:
            if truncated:
                handle.seek(size - _MAX_USAGE_BYTES)
            raw = handle.read(_MAX_USAGE_BYTES)
    except OSError:
        return [], False
    if truncated and b"\n" in raw:
        raw = raw.split(b"\n", 1)[1]
    rows = deque(maxlen=_MAX_LINES)
    for line in raw.splitlines():
        if len(line) > _MAX_LINE_BYTES:
            continue
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError):
            continue
        if isinstance(value, dict):
            rows.append(value)
    return list(rows), truncated


def _is_analysis_event(row: dict, run_id: str) -> bool:
    return (not run_id or row.get("run_id") == run_id) and \
        row.get("stage_id") == "campaign-analysis" and \
        row.get("event") in {"task_finish", "task_fail"}


def _cost_from_rows(rows: list[dict], run_id: str) -> float:
    value = 0.0
    for row in rows:
        if run_id and row.get("run_id") != run_id:
            continue
        if row.get("stage_id") != "campaign-analysis":
            continue
        usage = row.get("usage")
        cost = usage.get("cost") if isinstance(usage, dict) else None
        if isinstance(cost, dict) and isinstance(cost.get("total"), (int, float)):
            if math.isfinite(float(cost["total"])) and cost["total"] >= 0:
                value += float(cost["total"])
    return value


def _add_usage(total: dict[str, float], usage: dict) -> None:
    for key, value in usage.items():
        field = _TOKEN_FIELDS.get(str(key))
        if field and isinstance(value, (int, float)) and not isinstance(value, bool):
            if math.isfinite(float(value)) and value >= 0:
                amount = float(value) if field == "cost_usd" else int(value)
                total[field] = total.get(field, 0) + amount


def read_module_signals(root: Path, modules: Iterable[str]) -> dict[str, str]:
    """Read bounded, non-authoritative content hints for focus ranking."""
    signals = {}
    root = root.resolve()
    for module in modules:
        directory = (root / module).resolve()
        try:
            directory.relative_to(root)
            chunks = [module]
            for item in _bounded_signal_files(directory):
                try:
                    chunks.append(item.relative_to(root).as_posix())
                    chunks.append(item.read_bytes()[:_MAX_SIGNAL_BYTES].decode("utf-8", "ignore"))
                except OSError:
                    continue
            signals[module] = " ".join(chunks)
        except (OSError, ValueError):
            signals[module] = module
    return signals


def _bounded_signal_files(directory: Path):
    """Walk only a bounded deterministic prefix for focus hints."""
    pending = [directory]
    files = 0
    while pending and files < _MAX_SIGNAL_FILES:
        current = pending.pop()
        try:
            entries = []
            with os.scandir(current) as scan:
                for entry in scan:
                    if len(entries) >= _MAX_SIGNAL_ENTRIES_PER_DIR:
                        break
                    entries.append(entry)
        except OSError:
            continue
        for entry in sorted(entries, key=lambda item: item.name, reverse=True):
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
                    files += 1
                    if files >= _MAX_SIGNAL_FILES:
                        return
            except OSError:
                continue


def _rank(modules: tuple[str, ...], focus: str,
          priority: dict[str, int], signals: dict[str, str]) -> list[tuple[str, int]]:
    wanted = _tokens(focus)
    scored = [(module, len(wanted & _tokens(signals.get(module, module))) * 1000
               + priority.get(module, 0)) for module in modules]
    return sorted(scored, key=lambda item: (-item[1], item[0]))


def _tokens(value: str) -> set[str]:
    separated = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", value or "")
    return {item.lower() for item in _TOKEN_RE.findall(separated) if len(item) > 1}
