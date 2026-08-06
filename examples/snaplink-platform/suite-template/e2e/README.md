# E2E 场景清单（组合验证）

每个场景 = 一段可执行的脚本/断言，引用兼容矩阵中**真实服务版本**。
状态标注纪律：✅ 本机/CI 通过；🟡 仅部分验证；⛔ 未验收。

## 必测边界场景（平台级）

| # | 场景 | 涉及组件 | 状态 |
|---|---|---|---|
| 1 | 租户 A 无法访问租户 B 的文件 | snaplink, aero-vault | ⛔ |
| 2 | 用户冻结后，已有 Token 在规定时间内失效 | snaplink, aero-id | ⛔ |
| 3 | 管理员无法给自己授予受限角色 | snaplink, console | ⛔ |
| 4 | 删除用户时文件不会被意外物理删除 | aero-id, aero-vault | ⛔ |
| 5 | 审计系统不可用时业务事件不丢失（Outbox） | aero-vault, audit-governance | ⛔ |
| 6 | 消息系统不可用时不影响核心业务提交 | aero-im 降级路径 | ⛔ |
| 7 | 重复消费事件不重复冻结用户/不发重复信（幂等） | Event Bus 消费者 | ⛔ |
| 8 | 文件下载签名 URL 过期后不可用 | aero-vault | ⛔ |
| 9 | 撤销分享链接在 SLA 内失效 | aero-vault, snaplink | ⛔ |
| 10 | 权限变更后缓存正确失效 | snaplink | ⛔ |
| 11 | API Key 只能访问限定租户和 Scope | snaplink | ⛔ |
| 12 | 审计事件不能被普通管理员修改/删除 | audit-governance | ⛔ |
| 13 | 敏感字段不进日志/Trace/队列/审计（脱敏断言） | 全部 | ⛔ |
| 14 | 服务间 Token 不能冒充最终用户 | snaplink | ⛔ |
| 15 | 用户 Token 与 Service Token 审计主体可区分 | audit-governance | ⛔ |
| 16 | 前端隐藏按钮不能代替后端权限检查 | console, 各服务 | ⛔ |
| 17 | 并发角色修改不造成权限覆盖 | snaplink | ⛔ |
| 18 | 契约升级期间旧版本客户端仍工作 | 全部 | ⛔ |
| 19 | 审计事件乱序时仍能形成正确操作时间线 | audit-governance | ⛔ |
| 20 | 超级管理员操作具有额外认证和独立审计 | snaplink, console | ⛔ |

## 运行方式

```bash
# full 拓扑拉起后逐项断言（骨架）
docker compose -f compose/full.yaml up -d
pytest e2e/                      # 或 bash 脚本
```

每项验收通过后把状态改为 ✅ 并附日期与服务版本（来自 compatibility.yaml 窗口）。
