# ai-batch-runner 可观测性、多终端与自我优化设计

> 状态：提案/架构基线，不代表所有能力已经实现。
>
> 目标：让 `ai-batch-runner` 像真实开发团队一样，在每个小阶段完成时明确报告完成事实、下一目标、预计进度、阻塞原因和可调整项，并允许账号绑定多个 CLI/TUI/Web/移动端客户端，在不同执行设备之间安全调度和观察同一个任务。

## 1. 总体目标

`ai-batch-runner` 应从“本机调用 Agent 的批处理工具”演进为：

```text
多客户端控制面
  → 账号/租户/项目授权
  → Prompt / Run / Task Graph
  → 设备调度与执行
  → 统一进度/证据/成本账本
  → 指标分析与受控自我优化
```

核心闭环：

```text
Observe → Measure → Evaluate → Learn → Propose → Validate → Improve
```

这不仅是一个 Dashboard，而是一个以证据为基础的工程运行与反馈系统。

## 2. 当前已有能力与缺口

### 2.1 已有基础

仓库当前已经具备部分可复用能力：

- Campaign `state.jsonl` 和 `SUMMARY.md`
- session/memory 索引
- Token/usage metering
- append-only 交互事件账本
- Pipeline/Stage/Task/Gate 生命周期
- TUI 的 typed pipeline event observer
- `pi-batch ps --json`
- `pi-batch metrics --json`
- Device Fabric 的 Runner、心跳、Lease、Fencing Token、Capability Grant、迁移和 Checkpoint
- `devices`、`serve`、`runner` 等设备调度命令

### 2.2 目前缺口

以下能力尚未形成统一的跨账号、跨客户端、跨设备系统：

- 账号登录与客户端设备绑定
- 一个账号查看所有已授权客户端和执行设备
- 后台 Campaign 的统一实时进度 API
- 外部 TUI attach 到已在后台运行的 Campaign
- Web/移动端查看同一 Run 的状态与事件
- 阶段完成后的结构化 `next_goal` 和 ETA
- 统一的时间、Token、Agent message、Artifact、验证和返工指标
- 基于证据的优化提案、影子运行、对比和批准流程

现有 `serve` 主要是设备 Runner 控制平面，使用设备级签名/mTLS，并不是完整的账号级 Web/移动端控制 API。现有 TUI 主要观察当前进程或显式启动的 Pipeline，不能自动接管任意后台 Campaign。

## 3. 身份模型

必须分离以下身份：

```text
Account（用户账号）
  → Tenant / Project / Workspace
  → ClientDevice（手机、浏览器、CLI、TUI）
  → LogicalRun / Task
  → ExecutionAttempt
  → ExecutionDevice（Linux、Mac、GPU 节点等）
```

不能把登录客户端设备等同于执行设备，也不能让客户端仅凭传入 `device_id` 获得执行权限。

### 3.1 账号和客户端设备

- Web 使用 OIDC/OAuth2、Secure HttpOnly Cookie 和 HTTPS
- CLI 可使用浏览器登录或 device-code 登录
- 移动端令牌存放在 Keychain/Keystore/Secure Enclave
- 每个客户端拥有稳定的 `client_device_id`
- 支持显示设备名称、OS、App 版本、最后活动时间和登录位置
- 支持用户撤销单个客户端设备
- 所有登录、绑定、撤销、Prompt 提交、取消和审批写入审计账本

### 3.2 执行设备

执行设备由 Runner 注册并通过设备身份、证书指纹、能力探测和服务端策略认证。执行设备与客户端设备独立管理。

执行设备必须继续使用：

- 稳定设备身份，而不是 IP/主机名
- mTLS 或等价的设备认证
- 短期 Capability Grant
- 租约、Epoch、Fencing Token
- 数据级别与副作用级别限制
- 心跳、断线、Lost、迁移和 Checkpoint 语义

## 4. 统一 Run/Task 模型

建议建立以下事实实体：

```text
Account
ClientDevice
ExecutionDevice
Tenant
Project
Workspace
Run
Stage
Task
ExecutionAttempt
AgentMessage
ToolCall
Artifact
Validation
Gate
ProgressCheckpoint
Forecast
Decision
OptimizationProposal
```

逻辑 Agent、Task Node、Execution Attempt 和 Execution Target 必须分离。一个逻辑 Run 可以在不同设备产生多个执行尝试，但最终状态必须由控制面裁决。

## 5. 进度与证据事件

所有客户端消费同一份有界、追加式的事件流。事件应带有版本、序号和身份边界：

```json
{
  "schema_version": "progress.v1",
  "event_id": "evt-123",
  "seq": 42,
  "occurred_at": "2026-09-12T10:00:00Z",
  "run_id": "run-123",
  "stage_id": "design",
  "task_id": "task-4",
  "attempt_id": "attempt-2",
  "account_id": "account-1",
  "tenant_id": "tenant-1",
  "workspace_id": "workspace-1",
  "client_device_id": "phone-a",
  "execution_device_id": "linux-gpu-1",
  "event_type": "stage.completed",
  "status": "passed",
  "elapsed_seconds": 742,
  "next_goal": "design_gate",
  "evidence": ["artifact://artifact-7"],
  "validation": "passed"
}
```

事件类型至少包括：

```text
run.queued
run.started
run.paused
run.resumed
run.completed
run.failed
run.cancelled
stage.started
stage.completed
task.started
task.retrying
task.completed
agent.message.observed
tool.started
tool.completed
artifact.created
validation.started
validation.completed
gate.verdict
progress.checkpoint
forecast.updated
device.placed
attempt.lost
attempt.migrated
optimization.proposed
optimization.approved
```

### 5.1 持久化边界

- 中央控制面保存权威的 Run、Task、Attempt、状态、事件、指标和权限事实
- 执行设备保存有界本地 outbox，控制面恢复后重放
- Agent 原始 session 保留在 Provider 原生目录
- 中央事件只保存脱敏、有界元数据、摘要、Hash 和 Artifact 引用
- Prompt、Token、密钥和敏感正文不能进入运营日志或普通进度接口
- 单机模式可以使用 SQLite
- 多客户端/生产控制面应使用 HA PostgreSQL 或同等级受治理数据库
- 不能依赖多个设备共享可变工作目录作为事实源

## 6. 阶段完成报告

每一个阶段完成时，系统应根据 Pipeline DAG 和实际事件自动生成：

```text
✅ requirements 1/6 完成，用时 8m
下一目标：design
预计剩余：20–35m
置信度：medium
依据：过去 8 次同类阶段 P50/P90
阻塞：无
证据：requirements.md、validator=passed
```

结构化形式：

```json
{
  "stage": "requirements",
  "status": "passed",
  "completed": 1,
  "total": 6,
  "next_goal": "design",
  "next_acceptance": [
    "完成源数据到目标 DTO 的映射表",
    "确认无 Mock fallback"
  ],
  "eta": {
    "min_seconds": 1200,
    "max_seconds": 2100,
    "confidence": "medium",
    "basis": "historical_stage_p50_p90"
  },
  "blockers": [],
  "evidence": ["requirements.md"]
}
```

`next_goal` 必须从显式 Pipeline DAG、Gate 和未完成的验收条件中产生，不应完全依赖 Agent 自己描述。Agent 的建议可以展示，但必须标记为 `proposed`。

## 7. ETA 与预测纪律

ETA 只能使用可解释来源：

1. Pipeline 显式阶段预算
2. 同类阶段历史 P50/P90
3. 当前并发度和关键路径
4. 重试、限流、锁等待和设备排队
5. Provider、网络和执行设备健康度
6. 已完成的任务和剩余依赖

预测应保存实际误差，用于后续校准：

```text
forecast_p50
forecast_p90
actual_elapsed
absolute_error
interval_coverage
confidence
basis
```

没有足够样本时必须返回 `unknown`，不能生成虚假百分比或确定性完成时间。

## 8. 有效性与质量判断

“Agent message 是否有效”不能由正文长度、Token 数量或耗时单独判断。建议分层：

```text
received
→ structurally_valid
→ artifact_created
→ validator_passed
→ gate_verified
→ independently_reviewed
→ semantically_accepted
```

推荐状态：

```text
observed
proposed
structurally_valid
artifact_validated
gate_verified
accepted
rejected
partial
unknown
unavailable
stale
```

一条 Agent message 的价值证据可以包括：

- 是否生成了声明的 Artifact
- Artifact 是否通过 Schema/Validator
- 是否通过工程 Gate
- 是否被下游阶段实际消费
- 是否减少了返工或重复任务
- 是否被独立审查或人工验收认可
- 是否引入了失败、回滚或错误假设

正文不能伪造 Validator、Gate 或验收结果。

## 9. 指标体系

### 9.1 时间

- 排队时间
- Agent 执行时间
- Tool 调用时间
- Validator/Gate 时间
- 锁、网络和设备等待
- 阶段 P50/P90
- ETA 预测误差

### 9.2 成本

- input/output/cache Token
- Provider 调用次数
- 重试次数
- Provider 报告的费用
- CPU/GPU 时间
- Artifact 上传/下载量
- 网络流量和设备预算

Provider 未提供真实 usage 时记录 `null` 或 `unavailable`，不能伪装成真实数值。

### 9.3 质量

- 结构化输出通过率
- Artifact 生成率
- Validator 通过率
- Gate 通过率
- 返工率
- 被拒输出率
- 下游消费率
- 独立审查发现率
- 最终验收率

### 9.4 Agent 效率

- 重复消息和重复工具调用
- 无效上下文比例
- 输出被丢弃的比例
- 首次通过率
- 失败后的恢复率
- 角色超时率
- 角色的边际贡献
- Prompt 模板导致的返工率
- 模型/Provider/设备组合的质量差异

### 9.5 多设备

- 设备调度等待
- 冷启动时间
- 网络传输量
- 设备成功率
- 断线、Lost、迁移次数
- 不同设备验证结果差异
- 设备能力过期和探测失败

## 10. 多客户端 API

建议冻结以下控制面 API v1：

```http
POST /v1/runs
GET  /v1/runs
GET  /v1/runs/{runId}
GET  /v1/runs/{runId}/events?after=123
GET  /v1/runs/{runId}/metrics
GET  /v1/runs/{runId}/forecast
GET  /v1/devices
GET  /v1/client-devices
POST /v1/runs/{runId}/cancel
POST /v1/runs/{runId}/approve
POST /v1/runs/{runId}/retry
POST /v1/optimization-proposals/{proposalId}/approve
```

实时订阅：

```http
GET /v1/runs/{runId}/stream
```

使用 SSE 或 WebSocket，并支持 `after` 游标和事件序号，客户端断线后可以继续接收，不依赖屏幕进程内存。

默认控制 API 只绑定本机或内部 HTTPS；公网访问必须经过正常认证、反向代理和明确的 TLS/证书策略，不能直接公开本地 Runner 端口。

## 11. 客户端使用方式

### CLI

```bash
pi-batch login
pi-batch devices list
pi-batch run submit --prompt "分析这个项目" --target linux-gpu-1
pi-batch progress --run RUN_ID
pi-batch progress --run RUN_ID --follow
pi-batch metrics --run RUN_ID --json
pi-batch optimize proposals --project PROJECT_ID
```

### TUI

```bash
pi-batch tui
pi-batch tui --attach --run RUN_ID
```

建议增加：

```text
/progress
/runs
/devices
/forecast
/metrics
/optimize
```

### Web/移动端

Web 和移动端使用同一 API 与同一事件协议：

- 登录和客户端设备绑定
- Run 列表和筛选
- 当前阶段、下一目标、ETA 和阻塞原因
- Agent/Tool/Validator/Gate 时间线
- 设备选择和执行尝试
- Prompt 提交、取消、重试和审批
- 断线重连和历史恢复
- 推送通知

移动端第一阶段可以优先采用 PWA/响应式 Web，待 API、认证、设备和权限稳定后再构建 Android/iOS 原生 App。

## 12. 任务调度

用户可以从任一授权客户端提交 Prompt：

```text
手机/Web/CLI
  → POST /v1/runs
  → 控制面解析 Prompt、Pipeline 和约束
  → 生成 Task Graph
  → Placement Dry-run
  → Capability Grant
  → Runner Lease
  → 执行设备运行
  → 统一事件流
```

目标设备可以是：

- 指定设备
- 满足能力约束的任意设备
- 指定区域/集群
- 指定数据级别
- 指定不可迁移或可 Checkpoint 的任务

服务端必须根据账号、租户、项目、数据级别、副作用级别和设备策略裁决，不允许客户端绕过授权直接执行任意 Shell。

## 13. 自我优化闭环

系统经过足够运行样本后生成优化提案，而不是直接修改生产行为：

```json
{
  "proposal_id": "proposal-7",
  "kind": "routing_or_prompt_policy",
  "change": "减少 architect 角色的重复审查",
  "evidence": {
    "runs": 18,
    "duplicate_rate": 0.31,
    "gate_pass_change": 0.0,
    "median_time_saved_seconds": 420
  },
  "risk": "可能降低边界问题发现率",
  "mode": "shadow",
  "rollback": "恢复原角色配置",
  "approval_required": true
}
```

优化生命周期：

```text
observed
→ proposed
→ shadow
→ canary
→ compared
→ approved
→ promoted
→ monitored
→ rolled_back（必要时）
```

质量底线优先于资源优化：不能为了省 Token 或缩短耗时而降低安全、正确性、工程 Gate 或验收标准。

允许受控优化的方向：

- 阶段超时预算
- 重试与退避策略
- Meta 角色选择
- 上下文预算
- 模型/Provider 路由
- 设备放置和并发度
- 阶段依赖拆分
- 重复任务抑制

禁止无审计地自动修改生产代码、权限、数据库策略或外部副作用行为。

## 14. 推荐实施路线

### P0：统一事实模型

- 冻结 `progress.v1` JSON Schema
- 冻结 Run/Stage/Task/Attempt/Device/ClientDevice 身份
- 统一事件序号、游标和状态机
- 规定敏感信息、正文、Artifact 和 usage 边界

### P1：后台进度账本

- Pipeline/Campaign 发出 stage/task/retry/validator/gate/checkpoint 事件
- 持久化下一目标、ETA、阻塞和证据引用
- 实现历史 P50/P90 与预测误差
- `pi-batch progress --json/--follow`

### P2：TUI 和 Web 观察

- TUI attach 到后台 Run
- Web/SSE 只读查询 API
- 多客户端重连、筛选和历史恢复
- 账号、租户和客户端设备权限

### P3：控制操作

- 取消、暂停、恢复、重试、审批
- 目标设备选择和迁移
- 全部操作需要服务端授权和审计

### P4：移动端

- PWA 先行
- Android/Jetpack Compose
- iOS/iPadOS/SwiftUI
- 推送通知、设备撤销和安全存储

### P5：自我优化

- 指标物化和跨 Run 分析
- 优化提案
- Shadow/Canary 对照
- 批准、晋级和回滚

## 15. 验收标准

### 进度

- 任一阶段完成后，CLI/TUI/Web 都能看到同一个完成事实
- 下一目标来自 Pipeline DAG，而不是从自然语言猜测
- ETA 有范围、置信度和计算依据
- 无依据时显示 `unknown`

### 证据

- Agent 正文不能伪造 Validator/Gate/Acceptance
- 每个通过状态能追溯到事件和证据
- usage 不可用时显示 `unavailable`
- source、partial、stale 和 unavailable 状态不会被伪装成成功或零值

### 多设备

- 一个客户端提交的 Run 可从另一个授权客户端恢复查看
- 账号只能看到授权 Tenant/Project/Run
- 客户端设备和执行设备身份分离
- 设备撤销后不能继续接收新任务
- 失联、迁移、旧 Lease 和旧 Fencing Token 有可验证结果

### 自优化

- 优化提案包含样本、指标、风险和回滚方式
- 影子/灰度结果可与基线对比
- 未经批准不改变生产策略
- 质量 Gate 不因成本优化而绕过

## 16. 与跨平台实施的关系

这项能力应成为跨平台架构的核心，而不是在已有本地 CLI 外另做一个孤立 App。正确顺序是：

```text
账号/租户/客户端设备/执行设备身份
→ API v1 和事件协议
→ Run/Progress/Evidence 控制面
→ CLI/TUI/Web 共享客户端
→ PWA/移动端
→ Electron/Android/iOS 原生实现
```

因此，在 API v1、认证、租户隔离、EndpointResolver、证书信任和设备授权未冻结前，不应直接创建 Electron、Android 或 iOS 项目。跨平台架构 Gate 暂停是为了先完成这些事实和边界，而不是取消跨平台目标。
