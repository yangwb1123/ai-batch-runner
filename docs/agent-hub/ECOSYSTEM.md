# P3 生态集成

Hub 在已有会话与计算 API 上增加 Aero ID 成员投影门禁、Aero IM 状态通知和
Audit Governance 账本回执。服务默认关闭，管理员显式配置后启用。
成员资格是附加条件；原有 JWT scope、tenant、实例 grant 和计算目标范围仍必须匹配。

## 成员资格

Aero ID 新的只读服务接口依据已有成员投影判断资格，不创建账户或成员关系。
Hub 用 tenant、subject、client_id 三元组显式映射 Aero ID account_id，不根据
用户名、电子邮件或不同 OAuth client 的 subject 猜测账户是否相同。

配置及 Aero ID 服务绑定见 [IDENTITY.md](IDENTITY.md)。启用后，缺少映射、
非 active、同步错误、过期或无法查询的投影不能通过门禁。Hub 每次用户请求
都检查；Gateway 的委托计算使用原 prompt 持久化的用户 client_id 再次检查，
不能借机器主体绕过。启用门禁前创建、缺少该身份快照的 running turn 不能继续委托计算。

Aero ID 服务令牌还需携带签名 scope `aero-id.membership:check`。每次合法查询的
资格结果写入 Aero ID 现有持久审计账本；审计失败时返回 503，不放行未审计的结果。

这是按新鲜度约束的投影检查，撤销传播仍取决于上游同步频率。
已受理的任务继续遵循原任务状态机；门禁不会自动终止它们。Gateway 仍可回传
正在运行任务的状态与结果，避免撤权导致任务无法收尾。

## 状态事件投递

Hub 已有的持久事件日志是来源。每个 sink 的读取游标和新增 outbox 记录在
同一个 SQLite 事务内提交；投递目标、请求正文和幂等标识在网络请求前持久化。
只投影 turn/compute 生命周期，排除 prompt、聊天正文、命令 argv、工作路径、
stdout/stderr 和凭据。任务发起人与取消者分别记录，通知始终发给原发起者。

配置入口是顶层列表：

~~~yaml
integrations:
  - kind: im
    name: task-notices
    tenant_id: TEAM
    base_url: https://im.example.test
    token_file: /private/im-notify-token
    installation_id: REPLACE_INSTALLATION_UUID
    recipient_subject_map:
      REPLACE_HUB_SUBJECT: REPLACE_IM_USER_SUBJECT
  - kind: audit
    name: lifecycle-audit
    tenant_id: TEAM
    base_url: https://audit.example.test
    token_file: /private/audit-event-writer
    source_system: agent-hub
    schema_id: agent-hub.lifecycle
    event_type: agent.lifecycle
~~~

IM 使用现有受控 installation 通知 API，发布只含状态及 Hub 定位 ID 的 bot
卡片，不将收到的聊天转换成命令。installation 需要明确允许用户 DM，并绑定
可信用户 issuer 和机器 client。映射在 prepare 阶段冻结；重试发现接收人映射
已改变时失败，不会改投。配置细节见 [AGENT_HUB.md](../AGENT_HUB.md) 的 IM 小节。

Audit 使用已有 events API，管理员先注册受信任 source 与 payload schema。
仅 HTTP 202 accepted 不算投递完成；回执必须匹配事件和租户，并证明已入账
（ledgered/indexed/archived、有效入账时间、hash、stream 和 sequence）。
入账也不等同于 WORM 归档。注册模板、权限及字段说明见 [AUDIT.md](AUDIT.md)。

## 重试、回放与恢复

- 新 sink 默认从当前事件末尾开始。首次配置可显式加 start_cursor: 0 回放历史；
  后续重启从持久游标继续，不会因配置中的 start_cursor 改变而倒退。
- sink.name 在租户内唯一。端点、安装或审计 schema 等目标身份变化必须用新名字；
  同名配置漂移会拒绝启动，避免未完成事件被静默转移。
- 失败采用有上限的退避，每轮预算最多 12 次尝试；claim 后崩溃也计入。
  未完成的 sending 在重启或 90 秒恢复窗口后重新协调，过期尝试无法覆盖新回执。
- 手动 retry 提供新的 12 次预算，总尝试数保留。已准备的 payload 和幂等键不变；
  尚未准备成功的记录可在修复映射后重新准备。
- Aero IM 的服务端幂等记录保留 7 天，Hub 在首次尝试 6 天后拒绝继续发送，
  包括手动 retry，防止过期幂等键产生重复消息。
- 每租户最多 1,000 条、全局最多 10,000 条活跃投递。失败记录和已完成回执保留，
  不占活跃配额；达到配额时停止推进来源游标，稍后继续，不丢弃事件。
- 通知与审计使用独立于计算的 4 个有界 worker，每个 sink 同时一次投递；
  单个慢服务不阻塞其他 sink 的任务或 Hub 用户 API。源事件和回执目前不自动清理。

## 查询与人工重试

管理员的 OAuth token 与 Hub grant 都需增加 agent.integrations:read 和/或
agent.integrations:retry；Snaplink 如启用全局 scope registry，也须注册这两个 scope。
读取与重试均按租户和源实例过滤，机器 Gateway 不具备此操作权限。

~~~bash
python pi-batch.py fleet integrations deliveries --state failed --limit 100
python pi-batch.py fleet integrations deliveries --after 42
python pi-batch.py fleet integrations retry DELIVERY_UUID
~~~

沿用 PBATCH_HUB_URL 和私有 PBATCH_HUB_TOKEN_FILE。列表的 next_cursor 是
单调递增 sequence，传给 after 继续分页；delivery_id 是稳定 UUID，用于重试。
单独持有 retry 权限时只得到 delivery_id/state/attempts 回执，不返回事件或账本详情。

外部凭据只使用当前操作系统用户拥有的私有常规文件，每次请求重新读取以支持轮换。
传输使用 HTTPS（可配置 CA/mTLS）或字面 loopback HTTP，拒绝重定向和环境代理，
请求与响应大小均有限制。上游错误正文不会进入 Hub API 或投递记录。

本阶段没有向实际账号发送通知、注册 source/schema 或部署服务。
后续 GPU 增量已实现，见 [GPU.md](GPU.md)。Vault 工作区快照分发、原生安装包
和多 Hub 高可用仍未实现或验收。
