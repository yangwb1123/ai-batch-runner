评审已完成并写入 `docs/snaplink-platform/erp/reviews/identity_protocol_expert.md`。

## 本次实际运行的检查（独立复核）

- `bunx tsc --noEmit` → **31 个 TS 错误**（含 `request-middleware.ts:116/131/139` 三错，复现 B2）
- `bun test src/audit/` → 7 pass / 41 expects；`bun test src/auth/` → 25 pass / 1 skip
- `SNAPLINK_E2E=1` → **真实 sso-server 二进制 E2E 全绿**（31 expects：PKCE S256 + code 流 + Basic 认证 + userinfo + iss）
- `sqlite3 data/app.sqlite` → 无 `audit_outbox` 表；git 非仓库（复现 B4/B5）
- governance 仓库 `go build + go test` 全绿；`AUDIT_ALLOW_DEV_AUTH` 默认 true 已从 main.go 实证

## 关键协议发现（按严重度）

| # | 级别 | 要点 |
|---|---|---|
| P1 | High | **id_token 收到即存、从不校验**，nonce 从不发送——OIDC Core §3.1.3.7 MUST 违反（全新发现，不在 gate 裁决清单内，须补入修订清单） |
| P2 | High | 审计事件 actor 主体绑定丢失（H1 协议维度：`actor.id` 是接收端必填） |
| P3 | High | 投递语义：4xx 永久失败无限重试 + 批内 HOL 阻塞 + 409 语义缺失（SSF 投递原则违反） |
| P4 | High | **账本认证默认 fail-open**：`AUDIT_ALLOW_DEV_AUTH` 默认 true + 开发默认签名/加密密钥——削弱"审计事实真源"可信度 |
| P5 | Medium | 无 BCL/CAEP 接入，SSO 撤销后 ERP 会话最长存活 1h（生产 token_ttl）+ 下次请求间隔 |
| P6 | Medium | 契约类型级不兼容：`occurred_at` 整数毫秒 vs 契约 `format: date-time`；信封字段被批处理丢弃 |
| P7 | Medium | 刷新令牌轮换/重用零测试 |
| P8-P10 | Low/Info | ingest 路径命名空间冲突、不消费发现元数据（可接受）、平台 SSF/CAEP/SAML/SCIM 能力声明为不支持 |

## 结论

本审查**维持 gate 的 VERDICT: FAIL**：P1–P4 均为阻塞级协议证据，B1/B2/B3 独立复核后仍为 Verified。附 11 个先红后绿的优先级一致性测试（T1–T11）、声明不支持项清单，以及明确的不认证声明——全链无 OIDF 认证结果，互操作证据仅为真实二进制 E2E 与 governance 测试。
