# Backend System Engineering Spec — 消息/缓存/后台任务/限流/分布式锁

## 1. 消息队列语义（先明确再实现）

| 投递语义 | 使用 |
|---|---|
| 至多一次 | 可丢消息、不可重复（日志采样） |
| 至少一次 | 默认选择；消费者必须幂等 |
| 接近恰好一次 | 幂等消费 + 去重表/Inbox |

**消费者默认认为消息会重复投递**（处理成功但 ACK 前崩溃 → 重投）。
明确：分区键、顺序性（同分区有序）、重试队列 → 延迟重试 → 最大次数 →
**死信队列** → 人工/自动修复（毒消息不得无限重试）。
事件 = 业务事实（event_id/event_type/event_version/aggregate_id/
occurred_at/data），不直接发数据库对象。

## 2. 缓存一致性（禁止只写"加 Redis"）

明确模式：Cache Aside / Read Through / Write Through / Write Behind /
Refresh Ahead。必须回答：更新 DB 后何时删缓存、先删还是先改、删除失败
怎么办、重建并发、热点 Key、不一致可接受时长。常见策略：更新数据库 →
删除缓存；如仍并发读写，评估延迟双删是否真的适合（勿机械使用）。

## 3. 后台任务（禁止 is_running 布尔）

多实例防重复执行（分布式锁/租约/唯一任务表）→ 任务幂等 → 执行一半
崩溃恢复（进度持久化）→ 心跳 → 暂停/恢复/重跑 → 最大执行时间 →
分片 → 失败告警。长任务状态机：Pending → Running → Succeeded /
Failed / Retrying / Cancelled。

## 4. 限流（维度 + 算法 + 降级）

| 算法 | 特点 | 适用 |
|---|---|---|
| Fixed Window | 简单，窗口边界突发 | 粗粒度 |
| Sliding Window | 平滑 | 通用 |
| Token Bucket | 允许合理突发 | API 网关 |
| Leaky Bucket | 输出速率稳定 | 削峰 |

明确：维度（IP/用户/租户/API/操作）、单机还是分布式、Redis 不可用
降级（本地计数兜底）、是否返回 Retry-After、内部服务不同限额。

## 5. 分布式锁（先问能不能不用）

使用前必须分析替代：唯一约束？原子更新？队列串行化？分区单线程消费？
乐观锁？必须用时明确：锁 Key、租约时间、自动续约、所有者标识、
释放安全、进程暂停、网络分区、**Fencing Token**。仅 SETNX+EXPIRE
不是完整安全的分布式锁。

## 6. 优雅关闭（服务必须支持）

停止接收新请求 → 等待已有请求完成 → 停止拉取新消息 → 完成/释放任务 →
提交必要状态 → 关闭数据库/网络连接 → 退出。Readiness/Liveness/
PreStop/Shutdown Timeout。K8s 终止信号不得丢失处理中任务。

## 7. 一致性选择（跨服务）

同事务强一致：Unit of Work 在用例层；跨服务：Saga/Outbox/补偿（每步
幂等+可重试+可补偿+持久化状态+超时+人工干预）。禁止把长事务包住外部
网络调用。

## 8. 资源预算（禁止默认无限）

每个池有界：DB/HTTP/Worker/Goroutine/Promise.all/任务队列/缓存条目/
消息大小/单批次记录数——用 Semaphore/Bounded Queue/背压/拒绝策略。
一个报表任务不得耗尽共享连接池（Bulkhead 隔离）。
