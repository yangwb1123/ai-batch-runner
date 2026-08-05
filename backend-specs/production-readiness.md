# Backend Production Readiness Spec — 生产就绪门禁 + 系统可靠性

任务只有通过以下全部检查才能声明完成（machine 门禁 + 审查角色 + 人工
确认三通道）。本文件是**清单**，不是文章——审查角色逐项核对。

## 1. 需求与业务正确性（比代码正确性更重要）

- 业务目标、正常/异常/边界流程明确；区分"用户说出的方案"与"真正要
  解决的问题"（用户说"加状态字段"，先判断是状态还是审批阶段、单状态机
  还是多并行维度、是否需要历史/回退/权限）
- 业务不变量显式列出并落到多层保护：领域对象 + 应用层校验 + 数据库
  约束 + 并发控制 + 自动化测试（不只写文档）
- 权限与数据归属、重复提交与部分成功、可撤销性已评估

## 2. 契约设计（API/消息/事件/Schema 都是契约）

- 每个契约明确：字段/类型/必填/默认值/枚举/版本/兼容策略/错误语义/
  幂等语义/顺序与重复投递语义
- **事件禁止直接发数据库对象**：发布稳定业务事件（event_id/event_type/
  event_version/aggregate_id/occurred_at/data），考虑消费者兼容
- 兼容原则：新增可选字段通常安全；删除字段/收紧校验/改默认行为/改
  NULL 语义通常危险；枚举新增值也可能破坏旧客户端。编译通过 ≠ 兼容

## 3. 错误模型（禁止全 500 / 原样抛内部异常）

稳定错误模型：`{code: "ORDER_ALREADY_CANCELLED", message, trace_id, details}`。
分类：Validation/Auth/Authorization/NotFound/Conflict/RateLimit/
DependencyFailure/Timeout/ConcurrencyConflict/Internal；每类明确：
是否可重试、是否告警、是否补偿、是否暴露、HTTP 映射。

## 4. 可靠性（设计系统如何失败）

每个关键依赖（DB/Redis/MQ/第三方）定义：超时、重试、熔断、降级、
隔离、限流、回退、告警、恢复。覆盖：连接池耗尽、线程池满、磁盘满、
Pod 终止、消息重复/乱序、任务执行一半崩溃。

## 5. 容量与资源预算（禁止默认资源无限）

估算：QPS、并发连接、响应大小、DB 连接数、线程/协程数、队列长度、
缓存容量、消息大小、每日增长、日志保留。禁止每请求 20 个并行 DB 查询
（100 并发 × 20 = 2000 连接，池立即耗尽）。所有池有界：DB/HTTP/Worker/
Promise.all/Goroutine/任务队列——用 Semaphore/Bounded Queue/Rate Limiter/
Backpressure/Circuit Breaker。

## 6. 限流 / 缓存 / 消息 / 后台任务

- 限流：明确维度（IP/用户/租户/API/操作）+ 算法（Token Bucket 允许
  突发 / Leaky Bucket 稳速 / Sliding Window 平滑）+ 单机还是分布式 +
  Redis 不可用降级 + Retry-After
- 缓存：明确模式（Cache Aside 等）+ 更新后删缓存时序 + 删除失败 +
  重建并发 + 热点 Key + 不一致可接受时长
- 消息：明确投递语义（至多/至少一次）、分区键、重试队列、死信队列、
  毒消息上限（主队列→延迟重试→最大次数→DLQ→人工修复）；**消费者默认
  幂等**（成功但未 ACK 崩溃会重复投递）
- 后台任务：多实例防重、幂等、租约/心跳、暂停恢复重跑、执行进度、
  最大时长、分片、失败告警。长任务用状态机（Pending→Running→Succeeded/
  Failed/Retrying/Cancelled），禁止 is_running 布尔

## 7. 多租户与权限

- 隔离模式明确（共享表/Schema/独立库）；共享表时 `tenant_id` 必须进入：
  主查询条件、唯一索引、缓存 Key、事件、审计、对象存储路径、日志
- 禁止 `WHERE id = ?` 无租户条件（跨租户越权）；认证 → 功能权限 →
  资源权限 → 数据范围 → 字段脱敏 五层执行

## 8. 安全与隐私

- 输入不可信；SQL 注入/SSRF/路径穿越/命令注入/越权审查
- 敏感数据（密码/Token/证件/银行卡/生物信息）分类（Public/Internal/
  Confidential/Restricted）：加密存储、字段级加密、脱敏、日志白名单、
  保留期限、删除/匿名化、备份处理
- 密钥禁止进代码/YAML/Git；启动时校验、轮换、审计、回滚
- 供应链：新依赖必要性/License/已知漏洞/版本锁定/传递依赖；门禁
  npm audit / govulncheck / pip-audit / cargo audit / SBOM

## 9. 可观测性（日志/指标/Trace 三者分开）

- 日志回答"发生了什么"（结构化：level/operation/trace_id/tenant_id/
  error_code/retryable）；指标回答"多少次/是否恶化"（请求量/错误率/
  P50/P95/P99/DB 耗时/缓存命中/队列积压/熔断状态）；Trace 回答"经过哪些
  服务步骤"。敏感字段禁止入日志
- 核心请求/任务/消息携带 trace_id + correlation_id + tenant_id + user_id

## 10. 测试与验证

五层：单元（领域规则/算法）→ 集成（DB/缓存/消息/适配器）→ 契约
（API/消息兼容）→ E2E → 非功能（性能/安全/并发/故障）。补充：
Property-based（分摊和=总额）、Fuzz（解析器）、Race、Chaos。
性能优化必须有目标（QPS/P95/P99/内存/错误率）与基准测试，顺序：
先测量→找瓶颈→优化算法→减 I/O→优化查询→批处理→并发→缓存→硬件。
**未运行验证不得伪造结果**：报告必须区分 executed/passed/failed/
not_executed + 原因。

## 11. 发布与运行

- 优雅关闭：停收新请求 → 等已有请求 → 停拉新消息 → 完成任务 →
  提交状态 → 关连接 → 退出；Readiness/Liveness/PreStop/Shutdown Timeout
- 迁移支持安全部署（Expand–Migrate–Contract）、新旧共存灰度、回滚方案、
  数据修复对账、备份恢复（RPO/RTO/恢复演练）
- 关键模块 Runbook：如何确认正常/关键指标/常见故障定位/降级/重试/
  死信处理/回滚/修复错误数据
- 重大决策记 ADR（背景/问题/候选/选择/理由/代价/风险/重新评估条件）
- 技术债分类记录（必修/顺手/单独任务/风险可接受/暂不处理），禁止裸 TODO
