# 反例库（Anti-Patterns from Battle-Tested Projects）

> 两个实战项目（snaplink-console、iris-ui）用本工具跑出来的**真实踩坑
> 档案**。每条反例 = 场景 → 错误信号 → 正确动作。由 check-authority /
> check-evidence / check-baseline 门禁机械执行，其余靠 prompt 注入。

## AP-1 前端发明接口（authority-layering, L2）

- **场景**：snaplink-console 审计时间线需要 tenant_id/trace_id 关联。
  错误做法：前端自己造一条 BFF 通道（`no invented BFF`）。
- **错误信号**：产物声明能力但无契约出处；出现前端私有 API 路径；
  调用方"猜测"服务端接口而非引用目录条目。
- **正确动作**：能力声明必须带 file:line + 目录条目（OpenAPI/contracts）。
  B6 最终以 `SnaplinkAdminApi.get('/api/v1/audit/events')` 为唯一载体，
  目录 197 个精确操作之外的路径直接拒绝。

## AP-2 静态分析冒充验证（verify-by-doing, L4）

- **场景**：iris-ui 发布前只跑过 pnpm-workspace 符号链接下的测试。
- **错误信号**：验证只覆盖"自己的构建环境"；真实外部消费（npm install
  到工作区外、真实截图、真实浏览器）从未执行。
- **正确动作**：方向建议默认挂验证实验。check-pack-install 首次运行即
  暴露 Svelte ESM 扩展名缺失 + Solid 模块加载期 DOM 调用两个发布缺陷；
  行为契约 harness 三次抓到静态测试看不见的真实 bug。

## AP-3 伪成功 / 未验证数字进声明（evidence-discipline, L7）

- **场景**：产物声称"全部通过"但只跑了部分命令。
- **错误信号**："理论上应该通过"、"should pass"、"assume passed"；
  `[PROPOSED]` 断言进入验收声明；未执行命令无理由。
- **正确动作**：completion_report 逐命令记录 passed/failed/not_executed
  （check-completion-report.py 门禁）；`[PROPOSED]` 永不冒充 `[VERIFIED]`
  （snaplink 全程标注纪律）；"未验证的数字不进入状态声明"（iris-ui）。

## AP-4 无限重试同一失败（对抗收敛, L5）

- **场景**：gate 拒绝产物后盲目重试同一 prompt。
- **错误信号**：validator/gate 输出（报错、结构化发现）被丢弃；重试
  prompt 与上一轮逐字相同。
- **正确动作**：gate-fix 循环提取结构化发现 → 修复任务 → 重新裁决；
  验证器 stderr 回喂下一轮 prompt。

## AP-5 重复建议已被拒的方向（decision-memory, L10）

- **场景**：snaplink B6 的 630 行 state.jsonl 中大量
  `DIRECTION_REJECTED: duplicate, low score, missing evidence...`。
- **错误信号**：campaign 分析 prompt 不带历史已拒方向清单。
- **正确动作**：campaign 自动注入项目画像（含已拒方向与理由），
  新方向先查决策缓存（adr find）。

## AP-6 机械硬拆内聚文件（change-budget, L9）

- **场景**：iris-ui Table ×4 文件 1100-1512 行，机械拆 500 行会破坏内聚。
- **错误信号**：以行数硬门禁为目标重构；无测试保护下大重构。
- **正确动作**：baseline ratchet（只拦新超限与增长，不禁旧文件）；
  snaplink TECH_DEBT 判定"内聚的上帝文件保留"。

## AP-7 无门禁的代码生成（machine-gates, L3）

- **场景**：agent 生成代码但 pipeline 未配 validator，坏产物直接落盘。
- **错误信号**：代码类阶段 validate 为空且无 repo-scope 门禁。
- **正确动作**：`pi-batch doctor` 警告门禁缺失；验证失败零产物落盘。
