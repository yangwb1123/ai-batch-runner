# Identity Protocol Expert Review — sverpweb-stable 审计子系统与接入提案（OAuth/OIDC RP 行为 + 审计信号链路）

**审查对象**：`~/workspace/job/sverpweb-stable`（apps/api/src/audit/*、auth/*、routes/auth.ts）+ 上游 SSO 提供方 `/home/dwp/snaplink`（sso-server）+ 接收端 `/home/dwp/snaplink-audit-governance`。
**立场**：只对"身份协议面"下结论——协议合规、互操作、降级/oracle 风险、发现元数据、回放与幂等、失败分类。工程与合规交叉点引用但不重复 testing/compliance/gate 结论，仅在协议维度补充新证据。

## 0. 本次实际运行的检查（非转述）

| 命令（cwd） | 结果 |
|---|---|
| `bunx tsc --noEmit`（sverpweb-stable/apps/api） | ❌ **31 个 TS 错误**（`request-middleware.ts:116 TS2345`、`:131 TS2353`、`:139 TS2345`、`sqlite-outbox.test.ts:122` 均复现） |
| `bun test src/audit/` | ✅ 7 pass / 0 fail / 41 expects |
| `bun test src/auth/` | ✅ 25 pass / 1 skip（E2E 未设环境变量） |
| `SNAPLINK_E2E=1 bun test src/auth/snaplink.e2e.test.ts` | ✅ **1 pass / 31 expects**（真实 sso-server 二进制 `/home/dwp/snaplink/bin/sso-server`，真实 PKCE+code 流、Basic 客户端认证、userinfo/permissions/roles/menus） |
| `sqlite3 data/app.sqlite ".tables"` | ❌ 无 `audit_outbox` 表（复现 B4） |
| `git -C sverpweb-stable status` | ❌ not a git repository（复现 B5） |
| `go build ./... && go test ./internal/{auth,domain,service,httpapi}/...`（governance） | ✅ 全绿（含 auth JWT 校验、httpapi 路由/批处理） |
| 源码走查 | ERP `snaplink.ts`/`service.ts`/`crypto.ts`/`cookies.ts`/`sqlite-stores.ts`；SDK `hosted-login.ts`/`client.ts`；sso-server `server_discovery.go`/`server_oauth.go`/`server_backchannel_logout.go`/`server_logout.go`/`server_token.go`；`protocols/caep/doc.go`、`protocols/oidc/metadata.go`；governance `models.go:360`/`server.go`/`service.go`/`auth.go`/`cmd/audit-api/main.go`/`api/openapi/openapi.yaml` |

## 1. 协议/Profile 范围与权威引用

本集成链涉及两条协议面：

**A. OAuth 2.0 / OIDC RP（ERP 消费身份）**
- RFC 6749（授权码流 §4.1.1/§4.1.2/§4.1.2.1、客户端认证 §2.3.1、CSRF/会话固定 §10.12/§10.13）
- RFC 7636（PKCE §4.1–§4.5）
- RFC 9207（授权服务器 issuer 识别 §2/§2.1）
- RFC 7009（令牌吊销）、RFC 6750（Bearer 使用）
- OIDC Core 1.0（§3.1.2.1 授权请求参数、**§3.1.3.7 ID Token 校验**、§5.3 userinfo）
- OIDC Back-Channel Logout 1.0 §2.5 / RP-Initiated Logout（提供方已实现，RP 未消费）
- SSF（RFC 9493）/ CAEP（RFC 9500）/ SET（RFC 8417）：提供方已实现（`protocols/caep`），RP 未接入

**B. 审计信号链路（ERP outbox → audit-governance 账本）**
- **自定义 HTTP 契约**（governance `api/openapi/openapi.yaml` Event 组件），**不是**任何已发布标准协议；参考其设计意图与 SSF 的投递原则（幂等投递、retryable vs permanent 失败区分）做差距评审
- RFC 6750 §2.1 Bearer 认证 + JWT 校验（iss/aud/exp/nbf/sub/client_id/azp）已在 governance 侧实现
- 不适用/不在范围：SAML、SCIM（提供方有 `protocols/scim` 但 ERP 不使用）、WebAuthn（sso-server MFA/passkey，ERP 不触达）、DPoP/PAR/JARM（提供方能力，ERP 未使用）

**权威引用**：governance OpenAPI 是接收端契约的**唯一权威**（`api/openapi/openapi.yaml:232` Event 必填 13 字段）；sso-server 的 OIDC 元数据模型在 `protocols/oidc/metadata.go`（含 `authorization_response_iss_parameter_supported`、`backchannel_logout_supported`、`code_challenge_methods_supported`）。

## 2. 合规矩阵（节、要求、实现证据、状态、偏差、测试）

| # | 协议节 / 要求 | 实现证据 | 状态 | 偏差 / 测试 |
|---|---|---|---|---|
| A1 | RFC 6749 §4.1.1 授权请求（response_type=code、client_id、redirect_uri、scope、state） | `snaplink.ts:beginAuthorization` → SDK `hosted-login.ts`；E2E 断言 5 参数 | ✅ **Conform** | 无（E2E 31 expects 断言参数集） |
| A2 | RFC 7636 §4.3 PKCE S256（RP 侧 MUST 级建议 + 提供方 require_pkce） | `crypto.ts:createPKCE`（48 字节 verifier）；SDK 强制 `codeChallengeMethod: "S256"` 且拒绝 plain；E2E 配置 `require_pkce: true, allowed_pkce_methods: [S256]` | ✅ **Conform** | 无 |
| A3 | RFC 6749 §2.3.1 机密客户端认证 | `snaplink.ts:token()` 经 SDK Basic（client_id:secret）；E2E 断言 `Authorization: Basic ...` 且 body 无 client_id/secret | ✅ **Conform** | 无 |
| A4 | RFC 6749 §4.1.2/§10.12 state 绑定 + 常量时间比较 + 单次消费 | `service.ts:consumeAuthorization`（timingSafeEqual、长度预检）；`AuthorizationFlowStore.consume` 一次消费；TTL 5min | ✅ **Conform** | 单元测试覆盖 state 不匹配（snaplink.test.ts）；**无 code 重放测试**（见 T9） |
| A5 | RFC 9207 §2/§2.1 iss 参数（提供方 MUST 下发、RP MUST 校验） | 提供方：`server_discovery.go`（RFC 9207 §2 注释、授权响应 stamp iss）；RP：`routes/auth.ts` 要求 iss 必填、`service.ts:completeAuthorization` 严格等值比较 | ✅ **Conform** | E2E 断言 `loginBody.iss === baseUrl`；**无 iss 不匹配负向测试**（snaplink.test.ts 有 mock 覆盖） |
| A6 | OIDC Core §3.1.3.7 ID Token 校验（签名/iss/aud/exp/nonce，收到即 MUST） | `service.ts:createSession` 仅存储 `tokens.id_token`；**全链路无任何 id_token 校验**（无 JWKS 拉取、无 alg 固定、无 aud/exp/nonce 断言）；`snaplink.ts` 授权请求**不发送 nonce** | ❌ **Non-conform（High）** | 无测试（P1） |
| A7 | OIDC Core §5.3 userinfo 消费（Bearer + sub 必填） | `snaplink.ts:identity()` zod 校验 sub/name/…，`assertClientResponse` 校验 client_id 归属 | ✅ **Conform** | E2E 断言 userinfo 请求与 sub/roles/permissions/menus |
| A8 | RFC 7009 吊销（refresh_token + token_type_hint） | `snaplink.ts:logout()` → `sdk.postRevoke({token, token_type_hint: "refresh_token"})`；提供方 `revocation_endpoint` | ✅ **Conform** | **E2E 未断言吊销后刷新确实失败**（见 T10） |
| A9 | OIDC BCL 1.0 §2.5 / RP-Initiated Logout（提供方能力） | 提供方：`server_backchannel_logout.go`（§2.5 注释、重试 3 次、超时 5s）；**ERP 未注册 backchannel_logout_uri、未校验 logout token/SET** | ❌ **Non-conform（RP 侧缺失，Medium）** | 无（P5） |
| A10 | SSF/CAEP（RFC 9493/9500/8417）会话终止信号 | 提供方：`protocols/caep/doc.go`（SSF v1 transmitter、SET `typ=secevent+jwt`、按客户端作用域投递、refresh-token-family-reuse 事件）；**ERP 既不注册 receiver 也不校验 SET** | ⚠️ **Declared unsupported** | 无（P10） |
| A11 | 会话 cookie 硬化（httpOnly/SameSite/secure） | `http/cookies.ts`（httpOnly、SameSite=Lax、secure=COOKIE_SECURE）；`.env` 快照 `COOKIE_SECURE=false`（F-4 已提） | ⚠️ **Partial** | 生产须置 true |
| A12 | 刷新令牌轮换与重用检测 | `service.ts:refreshSession`（新 refresh_token 优先、旧值兜底）；提供方有 refresh-token-family-reuse CAEP 事件 | ⚠️ **Partial / Unknown** | 轮换语义与重用响应**无测试**（P7） |
| B1 | 接收端契约：Event 必填 13 字段（openapi.yaml:232） | ERP 映射缺 `schema_id`/`outcome`/`data_classification`/`retention_class`/`idempotency_key`/`actor`；`occurred_at` 为整数毫秒 vs 契约 `format: date-time`（RFC3339 字符串）；信封 `source`/`sentAt` 被 `batchRequest{events,wait_for}`（server.go:242）静默丢弃 | ❌ **Non-conform（High）** | client.test.ts 只断言信封（H3/M2） |
| B2 | `ValidateBasic` 必填 9 字段 + actor.id + schema_version>0（models.go:360-387） | 见 B1；`schemaVersion: 1` 存在但字段名/位置不兼容；`idempotency_key` 恒 null（`operation.idempotencyKey` 来自可选请求头） | ❌ **Non-conform（High）** | 无契约测试（B1 复证） |
| B3 | Schema 注册前置（validateEvent 要求 active schema，service.go:690-713，422 ErrSchemaNotFound） | governance 有 `POST /api/v1/schemas`；**ERP 侧无任何注册流程；governance 实例未部署** | ⚠️ **Missing**（部署前置未证明） | 无 |
| B4 | Bearer 认证（RFC 6750 §2.1）+ JWT 声明校验 | governance `auth.go`（Bearer 前缀、exp/nbf/iss/aud/sub、client_id/azp 一致性、scope/roles→permission 映射）；`require` 强制 `audit:event:write` | ✅ **Conform（接收端）**；⚠️ **ERP 侧 token 供给 Missing**（`AUDIT_APP_TOKEN` 为空，无 client_credentials/服务角色发放路径证据） | governance auth 单测绿 |
| B5 | 投递幂等/失败分类（SSF 投递原则） | `client.ts:publish` 任何 `!response.ok` 一律 throw → `AuditOutboxPublisher.flush` 指数退避（上限 5min）**永久重试**；无 4xx permanent 分类；`postBatch` 逐事件处理、首个失败即整体返回错误（HOL 阻塞） | ❌ **Non-conform（High）** | 无 4xx/5xx 分类测试（H3/H4） |
| B6 | 事件溯源完整性（hash 链） | governance `Ingest`（prev_hash/hash/stream/segment）；`protectSensitiveFields` | ✅ **Conform（账本内部）** | service 测试绿；**传输层 TLS 无证据**（F-4 明文 HTTP） |
| B7 | 账本认证默认值 fail-closed | `cmd/audit-api/main.go:44` `AUDIT_ALLOW_DEV_AUTH` **默认 true**；签名/加密密钥默认 `development-signing-key-change-me` / `development-encryption-key-change-me`；README.md:63 文档要求生产显式关闭 | ❌ **Fail-open（High）** | 无生产 profile 门禁测试（P4） |

## 3. Findings（按严重度）

### P1 [High] id_token 收到即存、从不校验；nonce 从不发送（OIDC Core §3.1.3.7 MUST 违反）
- **要求等级**：OIDC Core 1.0 §3.1.3.7（RP 收到 id_token 即 MUST 校验签名、iss、aud、exp、nonce）；§3.1.2.1（nonce 用于防重放，RP 应使用）。
- **位置**：`apps/api/src/auth/service.ts:createSession`（`tokens.id_token` 直存）、`snaplink.ts:beginAuthorization`（不传 nonce）；SDK `hosted-login.ts` 的 `nonce` 参数**从未被调用方使用**；`snaplink.test.ts`/E2E 均不断言 id_token 内容。
- **证据**：Verified（源码 + E2E 31 expects 中无任何 id_token 断言）。
- **互操作/安全影响**：当前身份路径实际走 userinfo（access token 绑定 PKCE 交换），id_token 不参与鉴权决策，因此**当前可利用性受限**；但会话中长期存有**未验证的第三方断言**：任何后续代码路径（SPA 令牌交接、step-up、会话接管检测）一旦消费该断言即放大为会话固定/重放面；且与严格 OIDC RP 审计（如等保/审计对 OIDC 声明的核查）不兼容。
- **纠正行为**：授权请求发送 nonce；收到 id_token 后按 §3.1.3.7 全量校验（JWKS 拉取、alg 白名单、iss/aud/exp/nonce、`sub` 与 userinfo 一致）；校验失败拒绝建立会话；先红后绿测试（见 T1/T2）。

### P2 [High] 审计事件丢失 actor 主体绑定（H1 的协议维度）
- **要求等级**：接收端契约 MUST（`ValidateBasic` 要求 `actor.id`）；审计问责语义要求"谁做的"可答。
- **位置**：`apps/api/src/audit/request-middleware.ts:116/131/139`（`withActor` 作用于 AuditEvent 而非 OperationContext；`actorDepartmentId/actorDepartmentSnapshot` 摊到事件顶层契约外字段；`operation.actorUserId` 恒 null）+ `contracts.ts:withActor` 签名只接受 OperationContext。
- **证据**：Verified（tsc 116/131/139 三错 + 代码走查 + `client.test.ts` 不断言 actor）。
- **影响**：即便契约字段映射修复，账本仍无法回答"哪个主体执行了操作"；L2 账本对治理/合规是**零问责价值**；同时顶层未知键会触发接收端 ValidateBasic 之外的 schema 门拒绝。
- **纠正行为**：`withActor(operation, ...)` 作用于 OperationContext 后再构造事件；`emitAuditEvent` 路径同修；测试断言 `event.operation.actorUserId === "user-1"` **且** `Object.keys(event)` 精确等于契约键集（gate 已采纳的"先红后绿"必须含键集严格断言）。

### P3 [High] 投递语义：永久失败无限重试 + 批内 HOL 阻塞 + 无 409 语义（SSF 投递原则违反）
- **要求等级**：投递契约 MUST（接收端 4xx=permanent / 5xx=retryable 区分；批处理不得以单个坏事件阻塞合法事件）；governance 已实现 409 幂等冲突语义（`event_id_content_conflict` / `idempotency_key_conflict`，service.go:361-379）。
- **位置**：`apps/api/src/audit/client.ts:66-80`（`!response.ok` 一律 throw）；`AuditOutboxPublisher.flush`（`retryBaseMs=2s`、`retryMaxMs=5min`、`2**min(attempts-1,8)` 退避，无上限轮次）；`server.go:postBatch`（逐事件 Ingest，首个错误即整体非 2xx）。
- **证据**：Verified（源码走查 + statusForError 映射 400/422/409/429）。
- **影响**：契约不兼容时每批 400 → outbox 永久 failed 积压（B1 已证）；**批内一个坏事件阻塞其后全部合法事件**（HOL）；409 冲突（同 event_id 不同内容 = 编程错误信号）与幂等去重成功（同内容重投）发送端无法区分，会无限重试或误报。
- **纠正行为**：按 gate 采纳项落地"4xx permanent 分类"（400/422/409 进终态 failed 或检疫区，503/408/429 重试且退避递增）；批级改为逐事件结果处理（合法事件入账、非法事件检疫）；`idempotency_key=event.id` 落地使 409→去重成功；H3/H4/H5 测试规格补入修订清单（testing_reviewer §5.2 已列，本审查附可执行断言）。

### P4 [High] 账本认证与完整性密钥默认 fail-open（"审计事实真源"宣称的前置缺口）
- **要求等级**：接收端生产部署 MUST fail-closed（README.md:63 自身要求）；账本真实性依赖签名密钥保密。
- **位置**：`/home/dwp/snaplink-audit-governance/cmd/audit-api/main.go:44`（`AUDIT_ALLOW_DEV_AUTH` 默认 true → `dev:<tenant>:<role>` 令牌可写事件）；main.go:94-95（`AUDIT_SIGNING_SECRET`/`AUDIT_ENCRYPTION_KEY` 默认 `development-*-change-me`）；`auth.go:parseDevToken`（dev 令牌绕过 JWT 签名校验）。
- **证据**：Verified（main.go 默认值 + 启动日志 `warning=development_auth_enabled`；**未运行任何生产实例**，故现网值 Unknown）。
- **影响**：任何拿到 `dev:sverp-web:event-writer` 格式令牌者（开发配置泄漏/内网可达）即可写入账本；已知开发密钥使账本 hash 链可被伪造方重算。直接削弱"governance 账本 = 审计事实真源"的可信度主张——L2 宣布前必须：`AUDIT_ALLOW_DEV_AUTH=false` 显式设置 + 密钥轮换 + 生产 profile 门禁测试。
- **纠正行为**：默认值翻转或部署检查强制显式设置；CI 门禁断言 production profile 无 dev 默认值（T7）。

### P5 [Medium] 无 SSO 侧会话终止传播（BCL/CAEP 未接入），ERP 会话可超期存活
- **要求等级**：OIDC BCL 1.0（提供方已实现 §2.5 语义）；CAEP（提供方已实现 push 撤销）。
- **位置**：ERP 未注册 `backchannel_logout_uri`、未校验 logout token；sso-server 生产配置 `token_ttl: 1h`、`session_ttl: 24h`（cmd/sso-server/config.yaml:47-48）；ERP 侧会话终止仅靠"下次请求时刷新失败"（`service.ts:getSession` 惰性刷新，刷新失败 <500 删除会话）。
- **证据**：Verified（配置 + 源码走查；E2E 配置 token_ttl 5m 故测试未暴露）。
- **影响**：SSO 侧吊销用户会话后，ERP 会话最长存活 **access token TTL（生产 1h）+ 用户下次请求间隔**；期间可继续用旧身份调用 ERP API（access token 有效期内不查 SSO）。身份协议维度看，这是"SSO 撤销≠应用撤销"的经典缺口。
- **纠正行为**：短期——接入 BCL（注册 backchannel_logout_uri，校验 logout token 签名/aud/sid）；中期——评估 CAEP receiver（`protocols/caep` 现成，SET 校验用同一 JWKS）；或至少把 ERP access 缓存压到分钟级并文档化残余窗口（T8）。

### P6 [Medium] 契约类型级不兼容与部署前置未证明（B1 的协议面补充）
- **要求等级**：接收端 OpenAPI 契约（Event 13 必填、occurred_at 为 `date-time` 字符串）。
- **位置**：`contracts.ts:occurredAt: number` vs openapi `format: date-time`；信封 `source/sentAt` 被 `batchRequest` 丢弃；schema 注册（422）、source_system↔client_id 绑定（`sourceAccessAllowed`）、tenant 映射（JWT `tenant_id`）均为**部署前置**，现网无任何证据。
- **影响**：即使字段映射完成，类型/格式不符仍整体 422；前置未注册时 422/403 与 B1 同效（账本零事实）。schemaVersion 兼容值（kickoff 待核点 1）与 token 供给（B4）均为 Unknown。
- **纠正行为**：M0 契约对拍先行（gate 采纳项）——golden payload 逐字段对拍 openapi.yaml；把 token 供给（client_credentials + `audit:event:write` scope 或服务角色）、schema 注册、tenant 分配列为显式前置清单并附负责人（gate B5 的 8 项 owner 决策之一）。

### P7 [Medium] 刷新令牌轮换/重用行为零测试（oracle/降级面）
- **要求等级**：RFC 6749 §6 刷新语义；提供方 CAEP refresh-token-family-reuse 事件（doc.go 明示"refresh-token-family reuse → that one client's receiver"）。
- **位置**：`service.ts:refreshSession`（新 token 优先、旧值兜底——轮换失败时保留旧值使重用窗口存在）；E2E 不覆盖刷新。
- **影响**：无法证明"重放旧 refresh_token 会被提供方拒绝并吊销全家"；若提供方未启用重用检测，被盗 refresh token 是永久会话等价物。
- **纠正行为**：E2E 增加：刷新一次 → 用旧 refresh_token 再刷新 → 断言被拒（或触发 family-reuse 撤销）；断言 ERP 会话随之删除（T10）。

### P8 [Low] 默认 ingest 路径与 sso-server 审计读接口命名空间冲突（错误配置面）
- **位置**：`env.ts:76` 默认 `AUDIT_INGEST_PATH=/api/v1/audit/events`；sso-server 存在 `GET /api/v1/audit/events`（读 API，测试 rootcov2_handlers_test.go:312）。
- **影响**：若 `AUDIT_APP_URL` 误指向 sso-server，POST 得到 405 → 永久重试风暴（P3 放大）。不构成当前缺陷（env 为空），但属于易踩的错误配置面。
- **纠正行为**：适配器落地时改路径为 `/api/v1/events:batch` 并加启动自检（URL 前缀可达性探测）。

### P9 [Low] 不消费发现元数据（可接受偏差，已 fail-closed）
- **证据**：RP 硬编码 issuer/baseUrl（`env.ts`），不拉取 `/.well-known/openid-configuration`；issuer 严格等值比较（无尾斜杠归一化）——错误配置时 fail closed，可接受；提供方元数据模型齐全（`protocols/oidc/metadata.go`）。
- **建议**：文档化"issuer 必须与提供方 `issuer` 完全一致（含尾斜杠）"，维持严格比较，不引入模糊匹配。

### P10 [Info] 平台级身份协议能力与 ERP 的差距（声明为不支持）
- 提供方已实现且 ERP 未使用：SSF/CAEP receiver（`protocols/caep`）、BCL（`server_backchannel_logout.go`）、DPoP/PAR/JARM（`server_token.go`/SDK 类型）、SAML/SCIM（`protocols/scim`、Connection 类型）、WebAuthn MFA。其中 **SSF/BCL 与 P5 直接相关**；其余对当前 ERP 集成不构成缺陷，列为"刻意未选用"并记录触发条件（安全基线升级、多 RP 撤销要求、审计事件需要标准流式出口时评估 AsyncAPI 生命周期主题）。

## 4. 优先级一致性测试（先红后绿）、声明不支持项、认证证据

### 4.1 优先级一致性测试（当前全部必红，除注明）
| # | 测试 | 断言要点 |
|---|---|---|
| T1 | id_token 校验（P1） | 授权响应含 id_token 时：JWKS 验签、alg 白名单（拒绝 `none`/HS256）、iss=配置 issuer、aud=client_id、exp>now、nonce=本次请求值、`sub`=userinfo.sub；任一失败→不建立会话 |
| T2 | nonce 发送（P1） | 授权 URL 含 43+ 字符 nonce；两次流 nonce 不同 |
| T3 | actor 绑定 + 键集（P2） | `event.operation.actorUserId === "user-1"`；`Object.keys(event)` 精确等于契约键集（无顶层 actorUserId/actorDepartmentId/actorDepartmentSnapshot） |
| T4 | 4xx/5xx 分类（P3） | 400/422/409→终态 failed 或检疫区（不重试）；503/408/429→retryable 且退避递增；fetcher 永不 resolve + timeoutMs=1→abort 路径 markFailed 且无 unhandled rejection |
| T5 | 批部分成功（P3） | 50 事件批含 3 个非法：合法事件最终 published、非法事件进 quarantine；`countPending === 0` |
| T6 | 409 幂等语义（P3） | 同 event.id 重投第二次 409 → 发送端不 markFailed（去重=成功）；瞬断重试（首投 503→重投 2xx）→ 接收端恰好一个事件 |
| T7 | governance 生产 profile 门禁（P4） | 无 `AUDIT_ALLOW_DEV_AUTH=false` 或签名/加密密钥仍为开发默认值 → 启动失败/CI 红 |
| T8 | BCL 接收（P5） | 注册 backchannel_logout_uri；收到 logout token（签名+aud+sid 校验）→ ERP 会话删除 |
| T9 | code 重放（A4/A5 补） | 同一 code 二次 exchange → 提供方拒绝；ERP 返回授权失败而非建立会话 |
| T10 | 刷新重用（P7） | 旧 refresh_token 重用 → 被拒（或 family-reuse 撤销触发）；ERP 会话删除 |
| T11 | 契约 golden payload（P6/B1） | 映射后载荷逐字段满足 openapi Event 13 必填 + `occurred_at` RFC3339 + `actor.id` 非空 + `idempotency_key=event.id`；governance `ValidateBasic` 全过 |

### 4.2 声明不支持（本集成范围）
- SAML、SCIM、WebAuthn、DPoP、PAR、JARM：提供方能力，ERP 集成不消费（触发条件另行评估）。
- SSF/CAEP receiver、BCL：**当前不支持**；P5 使 BCL 成为近期候选，CAEP 为中期候选（`protocols/caep` 提供方侧现成）。
- 审计事件的异步生命周期出口（governance AsyncAPI topics）：不接（沿用同步 ingest + outbox 重试）。

### 4.3 认证证据
- **不主张任何 OIDF 认证**：本审查覆盖的所有仓库（sso-server、ERP、governance）均未发布 OIDF 认证结果；本审查也未执行任何认证机构的对拍流程。提供方实现了大量 OIDC 特性，但**实现≠认证**。
- 已取得的互操作证据（本次运行）：真实 sso-server 二进制的 E2E（31 expects 全绿，覆盖 PKCE S256+code+Basic 认证+userinfo/权限/菜单+iss）；governance 全量单测绿（含 auth JWT 声明校验、批处理）；ERP audit 单测 7 绿（但仅正常路径）。
- 缺口证据：`pnpm check` 红（31 tsc 错误）、无 `forwarding.test.ts`、`audit_outbox` 表不存在、无 git 历史、governance 无生产实例、无 ERP→governance 端到端（M2 前置依赖未满足）。

## 5. 给修订阶段的协议面执行建议（超出既有裁决项的部分）

1. **P1（id_token/nonce）不在 gate 裁决清单内，必须补入修订清单**——它是纯协议合规项，与审计 L2 无耦合，可独立先做。
2. T11 契约对拍以 openapi.yaml 为唯一权威，**把类型级差异（occurred_at 整数 vs date-time）单列**，避免"字段名对上即兼容"的误判。
3. P4 是 governance 侧修复（默认值/门禁），应作为 **L2 宣布前置条件**写入 kickoff 修正（现 kickoff 无此约束）。
4. P5 的残余会话窗口（生产 1h）应写入部署文档的可接受风险清单，或在 L2 落地时同步评估 BCL。
5. 本审查不改变 gate 的 **VERDICT: FAIL** 判定——P1-P4 均为阻塞级协议证据，且 B1/B2/B3 在本审查独立复核后维持 Verified。

本评审为咨询性意见，不构成发布批准或任何认证声明。
