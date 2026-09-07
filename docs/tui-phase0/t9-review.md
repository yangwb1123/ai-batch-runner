# T9：Headless 只读审查

状态：已交付。命令行与 TUI `/review` 共用同一有界 finding 契约。

## 目标

把 Codex 的结构化 review 事件转换为有界、可机器读取的 findings 报告，并把
provider 自报的 file-change 事件作为只读契约告警。

## 已交付

- `pbatch/review.py`：实现 `pi-batch review`、Codex 子进程生命周期、有界 JSONL 解析、finding 归一化及 Markdown/JSON 报告。
- `pbatch/cli.py`：注册 `review` 子命令，shell completion 随注册表同步。
- `tests/test_review.py`：覆盖 scope、解析、退出码、超时、路径边界和 structured
  file-change 检测。

## 关键契约

- 审查范围支持 `--uncommitted`、`--base BRANCH`、`--commit SHA` 和自定义指令；互斥参数在启动 Codex 前拒绝。
- findings 统一为 `severity/file/line/problem/evidence/fix_hint`；未知严重度归一为 `medium` 并记录诊断，坏条目不影响其余合法 findings。
- 所有 stdout、stderr、正文和报告路径都有上限；输出只能写入当前工作区。
- 检测到 review JSONL 的 `file_change` item 时返回退出码 `3`；该检测不提供 OS 级
  写隔离，也不以 worktree before/after diff 发现未上报写入。
- 退出码：`0` 表示没有 critical/high finding；`1` 表示存在 critical/high finding；`2` 表示运行、超时或结构化结果失败；`3` 表示检测到写入。

## 验证入口

```bash
python -m pytest tests/test_review.py tests/test_pi_batch.py tests/test_entrypoints.py -q
python quality.py --strict .
python pi-batch.py eval
```

## 结果与边界

Headless review 已可作为 gate-fix 前的结构化发现来源；TUI `/review` 提供只读
scope 选择、结构化 finding 展示和报告落盘，`/findings` 可把安全归一化后的路径/行号
跳转到只读 Changes hunk。显式 App Server runtime 现在使用 inline `review/start`，
并回收有界 message/typed finding evidence；“修复此 finding”仍只启动普通 turn，
不自动修改或标记 finding resolved。普通 fix turn 结束后会生成 workspace 内的
`docs/reviews/review-fix-<id>.md`；只有 exact terminal RuntimeEvent scope 才会关联
source review、fix turn、validation、gate 与 authoritative workspace evidence，缺失
或不匹配时 fail closed 并明确标为 unavailable。后续仍需显式重新 `/review`。
