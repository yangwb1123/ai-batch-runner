# 关闭远端空闲会话并回收容量

CLI、TUI 和 Console 可关闭在线实例中的空闲会话。Hub 先记录请求并阻止新工作，
Gateway 确认底层运行时关闭成功后才回收会话名额。会话、turn、事件、计算结果和
工作区历史保留，关闭后的会话只读，不能继续发送 prompt。

## 使用

```sh
pi-batch fleet session-close SESSION_ID --idempotency-key close-001 --json
pi-batch fleet session-close-request REQUEST_ID --json
pi-batch fleet show SESSION_ID --json
pi-batch fleet session-create alpha --name '新的任务' --json
```

CLI 在发送前将本次 key 输出到 stderr。响应不确定时，用同一 session ID 和 key
重试；获得 request ID 后可随时查询。命令返回 `queued` 只表示已受理，只有
`closed` 表示 Gateway 已确认关闭并释放容量。

TUI 的 **Close session** 会先显示目标名称和确认按钮；**Keep session** 或 Esc
取消确认。**Retry close** 在回执未知时沿原 key 重试，已排队时只查询原请求。
查询失败暂停轮询，手动重试后继续。Console 的 Agent Operations 提供对应确认和
状态入口，Web 与原生 Flutter 客户端共用。请求期间切换会话不会改变请求目标，
迟到结果也不会抢占新的选择；原会话历史始终保留。

创建和关闭分别追踪一个未确认请求。UI 追踪保存在当前页面/进程内，重新登录、
离开页面或重启客户端不会持久保存恢复 key；请保留 CLI 的 key/request ID，或在
重新操作前检查会话状态。关闭弹窗或客户端不会取消已被 Hub 受理的关闭请求。

## 准入和权限

- Gateway 完成初始化后公布 `session_close_supported: true`。旧实例默认不支持；
  仅有 `session_capacity` 不代表能关闭会话。先升级 Hub，再升级 Gateway 和客户端。
- 关闭需要用户 `agent.sessions:write` 和目标实例 grant；查询需要
  `agent.sessions:read` 和同一实例 grant。启用 Aero ID 成员门禁时也必须通过。
  Gateway 始终使用精确实例的机器身份，不接收用户 token。
- 权限沿用实例共享管理：具备该实例写权限的用户可关闭其中任意空闲会话。
  创建请求的 actor/client 用于幂等身份，不表示会话独占所有权。
- 会话必须属于当前在线 generation 且可控。queued/running/cancel_requested turn
  或任意非终态计算任务存在时返回 `session_busy`。先等待完成，或通过既有取消入口
  请求取消并等待终态，再重新关闭。本入口不会强制停止活动任务。
- 准入检查和冻结在同一 SQLite 写事务内完成。新 prompt、compute 和工作区准入
  都会复读当前会话，因此不能在关闭受理后竞态插入新工作。

## 状态、幂等和失败恢复

| 请求状态 | 会话状态 | 容量与后续行为 |
|---|---|---|
| queued | closing、只读 | 保留名额，等待实际关闭 |
| closed | closed、只读 | 回收名额，可创建新的会话 |
| failed | close_failed、只读 | 保留名额，需要重启 Gateway 恢复 |
| lost | close_lost、只读 | Gateway 已换代；旧请求不重放，不宣称已关闭 |

请求 key 按 `(tenant, subject, client_id, key)` 唯一并绑定 session ID；同 key
改目标会冲突。相同 key 始终返回原请求当前状态，包括实例已离线或请求已终结。
每个会话最多一个关闭请求，不同 key 不能重复关闭、覆盖失败或重新开启会话。

Hub 的关闭账本决定容量释放；Gateway 普通 session 更新中的 `status='closed'`
不能伪造成功、释放名额或复活已冻结会话。成功回执与会话状态、容量更新在一个
事务内提交。ACK 丢失只重报持久收据，不再次调用 runtime.close。

关闭在独立单 worker 中执行，不在持有会话锁时调用底层 close，不阻塞其他会话的
心跳、prompt、取消和创建。等待创建 ACK 或上一轮线程收尾的会话会延后关闭。
关闭失败时对象可能已部分关闭，不能再次调用后把“已经关闭”的本地标记误认作
底层清理成功；本轮选择保留失败和名额，由 Gateway 重启后进入新的 generation。

Hub 重启可继续已持久化的队列。Gateway 换代把旧 queued 请求记为
`lost/owner_replaced`，旧历史保持只读；新 generation 正常计算自己的容量。
任意 Python provider 线程无法被本协议强制终止，关闭卡住时请求保持 queued，
不会提前报告成功或回收名额。

## 协议与存储

用户使用 `POST /api/v1/agent/sessions/{session_id}/close`（空 JSON 对象与必需的
Idempotency-Key）及 `GET /api/v1/agent/session-close-requests/{request_id}`。
Gateway 使用 `/api/v1/agent/gateway/session-close-requests` GET/POST，并绑定
instance/generation。完整协议见 [openapi.yaml](openapi.yaml)。

领域状态是不可逆的关闭请求；公开 DTO 仅投影 request/instance/session ID、状态、
固定错误码和时间。新增的 Hub 与 Gateway 收据表保存幂等与执行身份，唯一约束
防止重复请求，索引覆盖待处理队列；原有历史表不删除或重写。无需新增 OAuth scope。

本增量不实现会话删除、强制关闭、重新打开、迁移或物理设备部署。
本机验收及未执行项目见 [SESSION_CLOSE_VERIFICATION.md](SESSION_CLOSE_VERIFICATION.md)。
