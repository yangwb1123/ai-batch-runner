"""Pure read-only terminal projection of the shared public task snapshot. No I/O or control."""
from __future__ import annotations

import copy
import json
import re

from .task_center_io import display
from .task_attention import MAX_ITEMS, METHOD

KINDS = {
    "tasks": ("任务", "task_id", "tc", 200), "goals": ("目标", "goal_id", "g", 128),
    "intents": ("意图", "intent_id", "ih", 32), "repairs": ("恢复请求", "repair_id", "rq", 64),
    "resources": ("保留资源", "resource_id", "rr", 64), "feedback": ("反馈草案", "feedback_id", "fd", 32),
}
DISPLAY_KINDS = {**KINDS, "attention": ("关注事实", "attention_id", "at", MAX_ITEMS)}
MAX_DETAIL_BYTES = 128 * 1024
_SOURCE = re.compile(r"src-[a-f0-9]{24}\Z")
TASK_ID = re.compile(r"tc-[a-f0-9]{32}\Z")
_TERMINAL_CONTROL = re.compile(r"[\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def plain(value: object, maximum: int = 240) -> str:
    return _TERMINAL_CONTROL.sub(" ", display(value, maximum))


def admitted_sources(view: dict, source: str) -> set:
    values = view.get("sources")
    if not isinstance(values, list) or len(values) > 64:
        raise ValueError("terminal source window missing or unsupported")
    identities = set()
    for value in values:
        if (not isinstance(value, dict) or not isinstance(value.get("source_id"), str) or
                not _SOURCE.fullmatch(value["source_id"]) or not isinstance(value.get("name"), str)):
            raise ValueError("terminal source identity unavailable")
        if source and source not in {value["source_id"], value["name"]}:
            raise ValueError("terminal snapshot conflicts with the selected source filter")
        if value["source_id"] in identities:
            raise ValueError("ambiguous terminal source identity")
        identities.add(value["source_id"])
    return identities


def validate_options(limit: int, interval: float, task_id: str = "") -> None:
    import math
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("task limit must be between 1 and 200")
    if not math.isfinite(interval) or interval < 1:
        raise ValueError("interval must be finite and at least one second")
    if task_id and (not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id)):
        raise ValueError("terminal task selection needs a complete typed task ID")


def group_values(view: dict, kind: str):
    if kind != "attention":
        return view.get(kind)
    if kind not in view:
        return []  # old public snapshots do not acquire a fabricated zero/complete badge
    value = view[kind]
    if (not isinstance(value, dict) or type(value.get("schema_version")) is not int or
            value["schema_version"] != 1 or value.get("method") != METHOD or value.get("window_only") is not True):
        raise ValueError("unsupported attention projection")
    values = value.get("items")
    observed, omitted = value.get("observed_items"), value.get("omitted_items")
    if (not isinstance(values, list) or type(observed) is not int or type(omitted) is not int or
            omitted < 0 or observed != len(values) + omitted or value.get("grants_execution") is not False or
            value.get("zero_items_proves_safety_or_no_missing_work") is not False):
        raise ValueError("conflicting or unsupported attention counts/authority")
    return values


def records(view: dict, source: str = "") -> dict:
    if (not isinstance(view, dict) or type(view.get("schema_version")) is not int or
            view["schema_version"] != 1 or view.get("window_only") is not True):
        raise ValueError("unsupported shared task snapshot; no cached facts retained")
    sources = admitted_sources(view, source)
    result = {}
    for kind, (_, field, prefix, maximum) in DISPLAY_KINDS.items():
        values = group_values(view, kind)
        if not isinstance(values, list) or len(values) > maximum:
            raise ValueError("terminal record group missing, unsupported or over budget")
        pattern = re.compile(prefix + r"-[a-f0-9]{32}\Z")
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get(field), str) or not pattern.fullmatch(value[field]):
                raise ValueError("terminal record needs a full typed identity, never a display label or path")
            if not isinstance(value.get("source_id"), str) or value["source_id"] not in sources:
                raise ValueError("terminal record source identity unavailable")
            key = kind + ":" + value[field]
            if key in result:
                raise ValueError("ambiguous duplicate terminal record identity; no record selected")
            result[key] = safe_record(kind, value)
    return result


def attention_target(value: dict) -> None:
    target = value.get("target")
    if not isinstance(target, dict) or not isinstance(target.get("record_id"), str):
        raise ValueError("attention needs a complete typed target reference")
    kind, key = target.get("kind"), target["record_id"]
    if kind == "sources":
        valid = key == value["source_id"]
    elif isinstance(kind, str) and kind in KINDS:
        valid = re.fullmatch(KINDS[kind][2] + r"-[a-f0-9]{32}", key) is not None
    else:
        valid = False
    if not valid:
        raise ValueError("attention target identity unavailable or unsupported")


def safe_record(kind: str, value: dict) -> dict:
    if kind == "attention":
        attention_target(value)
    value = copy.deepcopy(value)
    if kind == "feedback" and not (value.get("status") == "local_draft_only" and value.get("state") == "draft"):
        value.update(summary=None, duplicate_inspection=None)
    if kind == "intents" and value.get("status") != "authorized":
        value["analysis"] = None
    json.dumps(value, ensure_ascii=False, allow_nan=False)  # refuse unsupported/nonfinite public values
    return value


def row_cells(key: str, record: dict) -> tuple[str, ...]:
    kind = key.partition(":")[0]
    _, identity = key.split(":", 1)
    title = record.get("title", record.get("statement", record.get("summary",
                record.get("relative_path_display", record.get("channel", "")))))
    state = record.get("status", record.get("acceptance", record.get("policy", "未追踪")))
    return (plain(record.get("project", record["source_id"]), 80), DISPLAY_KINDS[kind][0], identity,
            plain(state, 100), plain(title, 120))


def record_facts(key: str, record: dict) -> str:
    kind = key.partition(":")[0]
    if kind == "tasks":
        return task_facts(record)
    if kind == "feedback":
        matches = record.get("duplicate_inspection") or {}
        return ("本地合成声明；独立复现未完成；副本未实时复核。内容元数据候选=" +
                shown(matches.get("observed_matches")) + "；非同根因、非完整去重，未授执行/上传权。")
    notices = {"attention": "已有事实提示，不是根因、必做操作或干预授权；目标可能在任务显示窗口外。",
               "goals": "目标判定是本地记录/署名，不是独立验收或授权。",
               "intents": "词法线索仅是候选；非语义漂移结论、非执行计划；授权失效则不展示分析。",
               "repairs": "恢复请求/检查仍不可执行；原期限/预算、进程/lease 和工程/合并门禁未验证。",
               "resources": "保留计划；过期不是删除批准，已保存副本不证明完整可恢复归档。"}
    return notices[kind]


def shown(value: object) -> str:
    return "未追踪" if value is None else plain(value, 160)


def task_facts(record: dict) -> str:
    flow, timing = record.get("flow") or {}, record.get("time") or {}
    accounting = record.get("consumption") or record
    usage, billing = accounting.get("usage") or {}, accounting.get("billing") or {}
    amounts = [shown(row.get("reported_amount")) + " " + plain(row.get("currency"), 10)
               for row in billing.get("currency_totals", [])]
    delivery = record.get("delivery") or {}
    return ("上一观测=" + shown(flow.get("previous_observed_stage")) + "；当前=" +
        shown(flow.get("last_observed_stage")) + "；声明下一步=" + shown(flow.get("next_stage")) + "（非派发授权）\n" +
        "观测墙钟秒=" + shown(timing.get("elapsed_wall_seconds")) + "；token=" + shown(usage.get("total_tokens")) +
        "（" + shown(usage.get("token_coverage")) + "）；费用=" + ("; ".join(amounts) or "币种/费用未追踪") +
        "（" + shown(billing.get("financial_coverage")) + "；窗口内/关联范围可重叠，非全程账单）\n" +
        "产物存在/声明=" + shown(delivery.get("present_files")) + "/" + shown(delivery.get("declared_files")) +
        "；独立验收未验证；剩余时间/实时 ETA 未追踪。" + _prior_interval_line(record)
        + _mapping_line(record.get("execution_mapping"), "\n"))


def _prior_interval_line(record: dict) -> str:
    forecast = record.get("forecast") if isinstance(record.get("forecast"), dict) else {}
    interval = forecast.get("prior_remaining_interval_seconds")
    if (not isinstance(interval, (list, tuple)) or len(interval) != 2
            or any(isinstance(value, bool) or not isinstance(value, (int, float))
                   for value in interval)):
        return ("\n历史先验剩余区间=未追踪（"
                + shown(forecast.get("prior_remaining_basis") or forecast.get("reason")) + "）")
    return ("\n历史先验剩余区间=" + shown(interval[0]) + "–" + shown(interval[1]) +
            " 秒（同指纹回测先验减已观测时长；not_a_live_eta=" + shown(forecast.get("not_a_live_eta")) +
            "；remaining_seconds/eta 仍为 null）")


def _mapping_line(mapping: object, prefix: str = " ") -> str:
    if not isinstance(mapping, dict):
        return ""
    return (prefix + "执行映射：已验证=" + shown(mapping.get("verified_spawn_and_outcome")) +
            "；缺 spawn 证据=" + shown(mapping.get("outcome_without_spawn_observation")) +
            "（来源未验证，不是 0）；spawn-only=" + shown(mapping.get("spawn_only_active_observations")) +
            "（不是存活）；窗口=" + shown(mapping.get("coverage")) +
            "；计数为下界=" + shown(mapping.get("unknown_not_zero")) +
            "。不按标题/PID/时间戳推断，不代表验收或授权。")


def json_detail(value: dict) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
    raw = _TERMINAL_CONTROL.sub(lambda match: "\\u" + format(ord(match[0]), "04x"), text).encode("utf-8")
    if len(raw) <= MAX_DETAIL_BYTES:
        return raw.decode("utf-8")
    prefix = raw[:MAX_DETAIL_BYTES].decode("utf-8", "ignore")
    return prefix + "\n[只读显示已截断；不是完整 JSON，未追加读取原文件或授予执行权]"


class TerminalView:
    """Generation-bound display state. Invalidating never retains old details or rows."""
    def __init__(self, source: str = "", task_id: str = ""):
        self.source = source
        self.generation = 0
        self.selected = "tasks:" + task_id if task_id else ""
        self.items = {}
        self.snapshot = None
        self.message = "等待有界快照；活动不等于进展，未授执行/清场/上传权。"

    def invalidate(self, source: str | None = None) -> int:
        if source is not None and source != self.source:
            self.source, self.selected = source, ""
        self.generation += 1
        self.items, self.snapshot = {}, None
        self.message = "正在读取有界快照；不展示旧缓存记录。"
        return self.generation

    def publish(self, generation: int, view: dict) -> bool:
        if generation != self.generation:
            return False
        self.items, self.snapshot = {}, None
        try:
            items = records(view, self.source)
            frozen = copy.deepcopy(view)
        except (ValueError, TypeError, RecursionError) as exc:
            self.fail(generation, exc)
            raise ValueError("invalid terminal snapshot: " + plain(str(exc), 512)) from exc
        self.items, self.snapshot = items, frozen
        self.message = ("有界账本观测；产物/执行成功不等于验收，历史耗时不是实时 ETA。"
                        + _mapping_line(frozen.get("execution_mapping")))
        return True

    def fail(self, generation: int, error: object) -> bool:
        if generation != self.generation:
            return False
        self.items, self.snapshot = {}, None
        self.message = "数据不可用；未保留旧事实：" + plain(str(error), 512)
        return True

    def select(self, key: str) -> None:
        self.selected = key if key in self.items else ""

    def detail(self) -> str:
        record = self.items.get(self.selected)
        if record is not None:
            return json_detail(record)
        if self.selected and self.snapshot is not None:
            return "所选完整 ID 不在本次窗口；不代表已完成、已删除或仍有授权。"
        return self.message
