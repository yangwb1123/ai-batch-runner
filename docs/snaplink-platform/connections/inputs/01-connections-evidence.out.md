上下文已确认：这是 `connection-analysis-pipeline.yaml` 的 evidence 阶段任务（`from_dir: docs/snaplink-platform/connections/inputs`，每个输入文件一个任务，内容即 prompt）。`01-connections-evidence.md` 内容与您粘贴的一致（已核对字节级相同），无需落盘变更。日志显示上次运行在任务 1/4 被 SIGTERM 中断，现补跑本任务。

作为 evidence 阶段，我将这份代码级证据归一为供 kickoff 阶段直接消费的结构化摘要：

---

# 证据归一输出：6 项目连接现状（2026-08-08 代码级）

> 任务：`01-connections-evidence.md`（证据归一 · 只读消化，不产生新事实）。
> 证据等级：全部 **Verified**（来源为实际仓库代码：sverpweb `apps/api/src/integrations/clients/`、snaplink-console `lib/api/`、aero-vault 部署配置）。
> 端口权威表：snaplink=13015、console=13017、audit-governance=13018、aero-vault=13019、aero-id=13020、aero-im=13022、sverpweb=13014。

## A. 归一化连接清单（8 条，与 kickoff 阶段 A 项对接）

| # | 发起方 | 接收方 | 接口/协议 | 凭据 | 现状状态 |
|---|---|---|---|---|---|
| 1 | sverpweb | snaplink | OIDC code flow+PKCE（/api/auth/login、/token、/userinfo）、BFF cookie session、permissions claims | 服务账号：sso-admin（admin:*+audit+platform）、sverp-audit（audit:*+platform）、sverp-vault（read/write/admin） | **已对接**：BFF 代理 + @snaplink/sso-client SDK（SnaplinkTokenClient 统一 token） |
| 2 | sverpweb | audit-governance | POST /api/v1/events:batch、GET /api/v1/events、POST /api/v1/schemas | sverp-audit（audit:event:write）、sso-admin（audit:policy:write） | **已对接**：SQLite outbox → governance-transport 映射 → 批量；permanent 毒丸隔离+幂等重放；338 事件全 published；schema 每事件注册 |
| 3 | sverpweb | aero-vault | presign（POST /v1/files/:key/presign?op=put\|get）、/v1/files CRUD、metadata、/v1/access/acl、/v1/admin/departments(+members) | sverp-vault（read write admin scope） | **已对接**：presign 直传（BFF 不缓冲）、受控引用双重校验、部门 ACL 实时、质检附件入 vault |
| 4 | sverpweb | aero-im | POST /api/rooms/:id/messages、ensureRoom/login | aero-im 应用内登录（app token） | **已对接（粗粒度）**：best_effort 通知，房间广播（审批/质检异常/文件共享变更） |
| 5 | snaplink-console | snaplink | GET /api/v1/admin/endpoints（能力发现）、/api/v1/admin/*、/api/v1/audit/events、/api/v1/scim/* | admin 用户 OIDC token（admin scope） | **已对接**：SnaplinkAdminApi（Flutter 浏览器直连 13015） |
| 6 | aero-vault | snaplink | GET :13015/.well-known/jwks.json（JWT 验签）、tenant claim 映射 | 服务账号 JWT；配置 AUTH_JWT_ISSUER=…:13015、AUTH_JWKS_CLIENT_TENANTS=sverp-vault:sverp | **已对接**：vault 侧验签 |
| 7 | aero-im / aero-id / audit-governance | snaplink | JWKS 验签（各服务 AUTH_JWT_ISSUER/JWKS_ENDPOINT） | — | **部分**：aero-im 走 JWT 验签；aero-id gRPC（snaplink:token:validate）**未定案**；governance 用 OAuth 服务账号 |
| 8 | snaplink-console | 各服务（governance 等） | nginx BFF 代理（/tmp/snaplink-console/proxy） | 两 token 模型（用户 OIDC + 服务账号） | **未落地**：scope-matrix 提案推进中 |

## B. 信任边界（归一）

- **JWKS 信任链**：snaplink 为唯一签发方；vault/im/id/governance 均为验签方（连接 6、7）。
- **服务账号最小权限现状**：sso-admin（管理/审计/平台三域）、sverp-audit（审计写+platform）、sverp-vault（文件三 scope）。凭据与 scope-matrix 草案（04-scope-matrix.md）的注册表 seed 命名一致（vault:blob:*/audit:event:ingest/account:uid:resolve 为 [PROPOSED]，本证据均为已实装名称）。
- **控制面缺口**：console 浏览器直连 snaplink（连接 5）违反组合纪律中 `console-browser-bypasses-service-api` 禁令；连接 8 的 BFF 化正是处置方向。

## C. 连接缺口清单（已观察事实，供阶段 3 展开）

1. **aero-im 无 Outbox relay**（05-integration-design.md 标为 verified gap；scope-matrix flow 3 将 `audit:event:ingest` 授予设为条件）。
2. **aero-id gRPC 校验未定案**（snaplink:token:validate vs mTLS allowlist，scope-matrix 开放决策）。
3. **console 两 token 模型未落地**（连接 8；治理账本查询路径待定——当前审计时间线直连 snaplink）。
4. **governance 读侧无专用查询账号**（sverp-audit 仅写 scope，查询走 sso-admin policy scope——最小权限存疑）。
5. **sverpweb→aero-im 仅房间广播**（无 DM/定向通知能力接入）。
6. **schema 注册靠健康检查事后发现**（无前置审批链）。

## D. 证据质量与边界

- 本证据覆盖 **8 条连接中的出站/入站面**，但未覆盖各服务内部实现细节（sverpweb 内部 338 事件清单、vault ACL 实时同步机制等细节在阶段 3 按项目展开时需回到各自仓库核实）。
- 与 `02-platform-map.yaml` 一致：sverpweb-stable 的 sso-login/audit-outbox 均 Verified；snaplink 无审计上报出站（自身为权威源）；console 不拥有业务数据。
- 缺口清单与 `04-scope-matrix.md`、`05-integration-design.md` 的开放决策无冲突（均为 [PROPOSED] 待批，本证据不裁决）。

**归一结论**：8 条连接中 6 条已对接（1/2/3/4/5/6）、1 条部分（7，aero-id gRPC 未定案）、1 条未落地（8，BFF 两 token）。证据可直接供 kickoff 阶段生成连接矩阵与缺口清单。
