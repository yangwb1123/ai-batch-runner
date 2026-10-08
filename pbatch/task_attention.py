"""Pure bounded attention facts from already admitted snapshots/candidates, never intervention authority."""
from __future__ import annotations

from collections import Counter
from heapq import nsmallest

from .task_center_adapters import identity
from .task_center_io import display

MAX_ITEMS = 128
METHOD = "bounded-attention-facts-v1"
_TASK_STATES = {
    "failed": ("reported_failure", "记录了失败观测；原因/当前可恢复性未验证"),
    "rejected": ("reported_rejection", "记录了产物拒绝；不复用旧成功或降低门禁"),
    "pipeline_failed": ("reported_pipeline_failure", "记录了流水线失败；不是重新开始的指令"),
    "gate_rejected": ("reported_gate_rejection", "记录了 gate 拒绝；原门禁仍保留"),
    "validation_failed": ("reported_validation_failure", "记录了验证失败；不等于已证明根因"),
    "merge_blocked": ("reported_merge_block", "记录了合并阻断；未授合并权"),
    "budget_blocked": ("reported_budget_block", "记录了预算阻断；未核验剩余预算、不重置限制"),
    "provider_breaker_paused": ("reported_provider_pause", "记录了 provider 暂停；不猜测额度原因或复位 breaker"),
    "recovery_blocked": ("reported_recovery_block", "记录了恢复阻断；保留现场、不重启"),
}
_METADATA = {
    "goals": ("goal_id", "acceptance", {"operator_rejected": ("goal_record_rejected", "本地目标判定记录为拒绝；署名不等于独立验收"),
        "evidence_not_current": ("goal_evidence_not_current", "目标记录的证据不再匹配或不可用；不沿用旧判定")}),
    "intents": ("intent_id", "status", {"stale_contract": ("intent_stale_contract", "历史授权的契约已变化；线索已停止展示"),
        "goal_unavailable": ("intent_goal_unavailable", "历史对应目标在视图中不可用；不猜测已删除或仍有授权")}),
    "repairs": ("repair_id", "status", {"expired_declaration": ("repair_declaration_expired", "本地恢复请求声明过期；不是原执行期限/预算的核验")}),
    "resources": ("resource_id", "policy", {"review_due_preserve": ("resource_review_due", "资源登记复核日期已到；仍保留，不是删除批准")}),
    "feedback": ("feedback_id", "status", {"stale_source_definition": ("feedback_stale_source", "草案来源定义已变化；摘要/关联不可沿用，不导入新正文")}),
}


def _item(source: dict, kind: str, key: str, code: str, note: str, evidence: dict) -> dict:
    source_id = source["source_id"]
    return {"attention_id": "at-" + identity([source_id, kind, key, code]), "source_id": source_id,
        "project": display(source.get("project", source.get("name", "")), 80),
        "target": {"kind": kind, "record_id": key}, "reason_code": code, "summary": note,
        "status": "observed_attention_fact", "basis": "already_admitted_bounded_metadata_not_live_preflight",
        "evidence": evidence, "root_cause": "not_verified", "operator_action": "not_selected_or_authorized",
        "capabilities": {"control": False, "resume": False, "merge": False, "cleanup": False,
                         "feedback_upload": False, "budget_or_deadline_reset": False}}


def _task_items(candidates: list):
    for _, key, source, rows, partial in candidates:
        last = rows[-1]
        selected = _TASK_STATES.get(last["status"].lower())
        if selected:
            code, note = selected
            evidence = {"record_ref": display(last["record_ref"], 240), "reported_status": last["status"],
                        "phase": display(last["phase"], 80), "partial_window": partial}
            yield _item(source, "tasks", "tc-" + key, code, note, evidence)


def _source_items(sources: list):
    for source in sources:
        gaps = [{"feed": display(feed.get("feed"), 80),
                 **{key: feed.get(key) for key in ("truncated", "incomplete", "malformed")},
                 "read_error_observed": bool(feed.get("error"))}
                for feed in source["coverage"] if any(feed.get(key) for key in ("truncated", "incomplete", "malformed", "error"))]
        if gaps:
            yield _item(source, "sources", source["source_id"], "source_feed_gap", "来源账本有读取诊断/损坏/窗口缺口；未知不是零", {
                "feeds": gaps[:8], "omitted_feed_refs": max(0, len(gaps) - 8), "live_process_liveness": "not_verified"})


def _metadata_items(view: dict):
    for kind, (field, state, choices) in _METADATA.items():
        for value in view[kind]:
            selected = choices.get(value[state])
            if selected:
                code, note = selected
                yield _item(value, kind, value[field], code, note, {"recorded_state": value[state],
                    "record_version": value["version"], "additional_payload_reverification_performed": False})


def attention_view(view: dict, candidates: list) -> dict:
    """Aggregate all admitted candidates before task display trimming; retain bounded stable-ID references."""
    reasons, kinds = Counter(), Counter()
    def measured():
        for stream in (_task_items(candidates), _source_items(view["sources"]), _metadata_items(view)):
            for item in stream:
                reasons[item["reason_code"]] += 1
                kinds[item["target"]["kind"]] += 1
                yield item
    items = nsmallest(MAX_ITEMS, measured(), key=lambda row: row["attention_id"])
    observed = sum(reasons.values())
    return {"schema_version": 1, "method": METHOD, "items": items, "observed_items": observed,
        "omitted_items": observed - len(items), "counts_by_reason": dict(sorted(reasons.items())),
        "counts_by_kind": dict(sorted(kinds.items())), "window_only": True,
        "coverage": {"task_candidates": len(candidates), "source_records": len(view["sources"]),
            "snapshot_diagnostics": len(view["diagnostics"]), "task_window_partial": any(row[4] for row in candidates),
            "basis": "admitted_candidate_windows_before_task_limit_and_selected_metadata_not_lifetime_or_complete_inventory"},
        "ordering": "stable_full_attention_id_not_severity_recency_or_efficiency_ranking",
        "grants_execution": False, "zero_items_proves_safety_or_no_missing_work": False,
        "note": "observation cues only; counts are facts, not distinct defects, root causes or mandatory operator actions; no new reads, clocks, probes, writes or dispatch"}
