# T1 — Phase 0 协议地基：RuntimeEvent / Capabilities / Turn 状态机 核验报告

核验日期：2026-08-22 · 驱动：`pi-batch.py tasks/tui-phase0-t1.yaml --agent-bin codex`
基线对象：commit `0724534`（Phase 0 protocol foundation）之上、HEAD `52809c4` 的当前实现。

## 1. 交付清单

| 文件 | 行数 | 说明 |
|---|---|---|
| `pbatch/tui_protocol.py` | 399 | 纯数据协议层：`RuntimeEvent` / `RuntimeCapabilities` / `TurnState` 状态机 / seq 单调与幂等去重辅助 |
| `tests/test_tui_protocol.py` | 233 | 协议契约测试（schema round-trip、未知 kind/字段、迁移表、超限、能力默认值） |
| `docs/tui-phase0/t1-protocol.md` | 本报告 | 完成核验报告 |

## 2. 逐项对照（设计 §7 / §12 / §13.1 → 实现）

| 验收项 | 结论 | 依据 |
|---|---|---|
| 1. `RuntimeEvent` 字段与 kind 固定枚举 | ✅ | 12 个协议字段齐全；未知 kind 构造抛 `ValueError`（fail closed）；`extensions` 按 UTF-8 编码 ≤16KiB |
| 2. `to_jsonl()`/`from_jsonl()` round-trip | ✅ | 单行 JSON round-trip；未知顶层字段原位保留（schema 演进，§13.1）；坏 JSON/超长单行/非 dict 抛 `ValueError` |
| 3. `RuntimeCapabilities` 默认值 | ✅ | 富能力默认 `False`；Legacy 至少 `message_stream`/`interrupt=True` |
| 4. `TurnState` 迁移表 | ✅ | 与 §7.2 完全一致；终态（SUCCEEDED/FAILED/INTERRUPTED/UNKNOWN）不可再迁移；表外迁移抛 `ValueError` |
| 5. seq 单调与幂等去重 | ✅ | 同 run 内 seq 严格递增；重复 `event_id` 由 `is_duplicate()` 判定跳过而非报错 |
| 6. 大小上限 / `event_id` 格式 | ✅ | payload 序列化 ≤64KiB 拒绝；`event_id` 匹配 `^evt_[A-Za-z0-9_-]+$` |
| 7. 零依赖 / 无 IO / 组织门禁 | ✅ | 仅标准库；文件 399 行 ≤500、函数 ≤50 行、圈复杂度 ≤15（strict quality：OK） |
| 8. 不触碰接线文件 | ✅ | `git diff HEAD --stat -- pbatch/tui.py pbatch/tui_backend.py pbatch/runner.py` 为空 |

## 3. 测试与门禁结果

```
python -m pytest tests/test_tui_protocol.py -q             → 148 passed in 0.11s
python -m pytest tests/test_tui_adapter_codex.py \
                 tests/test_tui_adapter_appserver.py -q    → 22 passed（回归联动）
python quality.py .                                        → quality: OK（基线）
python quality.py --strict .                               → quality: OK
```

## 4. 设计文档与实现之间的偏差说明

1. **§7.3 首版枚举 vs §9.3 `runtime.unknown_item`（唯一实质性偏差）**：T1 验收范围
   列出的"首版事件类型"严格按 §7.3，不含 `runtime.unknown_item`；但 §9.3 明确要求
   "未知 item 保留为 `runtime.unknown_item`，不导致整个 run 崩溃"。此后 Phase 1
   （commit `7daf875`）已按 §9.3 实现该扩展，并落地于 `tui_adapter_codex.py` /
   `tui_adapter_appserver.py` 及其测试。本次复跑若按字面把枚举缩回 §7.3 首版集合，
   会直接破坏这 2 个已合并适配器的契约测试（`ValueError: unknown runtime event
   kind`）。**结论：维持 §9.3 扩展（`runtime.unknown_item` 在固定枚举内），
   视为对 §7.3 首版集合的已批准演进，并在本报告记录在案。**
2. **复跑出的非线性教训**：本次 codex 驱动复跑曾把枚举缩回 §7.3 首版集合并删除
   `runtime.unknown_item`，虽协议自身测试与 quality 通过，但经回归联动验证发现
   破坏 Phase 1 适配器，已恢复（`git checkout HEAD --` 两个文件）——证明 T1 这类
   跨阶段任务复跑时必须带"下游依赖回归"视角，不能只看自身门禁。
3. **输出文档形态**：按仓库既有惯例，codex 驱动的任务输出为 JSONL transcript；
   本报告为核验结论文档，替代原 transcript。

## 5. 结论

T1 交付物（`pbatch/tui_protocol.py` + `tests/test_tui_protocol.py`）满足设计
§7/§12/§13.1 的全部契约；`runtime.unknown_item` 扩展与 §9.3 一致并已被 Phase 1
消费，属于设计文档内部层级差异而非实现缺陷。全部门禁通过。