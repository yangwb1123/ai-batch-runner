# QA Lead Review — v2 Audit Rollout（risk-based test review）

> **Revision**: B0（COMPOSE-2026-017 之后、各批次实现之前）。审查对象 = 6 仓库
> 当前 HEAD 的**测试现状**与 `v2/implementation-batches.md` 全部验收断言
> （T-1.1…T-12、D1、L0、P2 parity、容量 envelope、G1–G8）的**可测性**。
> 方法与证据标准遵循 `prompts/README.md`：本报告只报告本次实际运行的命令
> 及其结果；`[Verified]` = 本修订实测；`[Partial]` = 代码/测试存在但断言未覆盖；
> `[Missing]` = 无测试且（或）无实现；`[Proposed]` = 计划中的测试。

## 0. 测试套件分层（default CI vs tagged vs manual）

| 层 | 定义 | 现状 |
|---|---|---|
| **默认 CI** | 每仓 CI 每次运行 | sink：**无 CI workflow**（`make ci` = `cli.py ci` 本地门禁，实测 PASS）；aero-vault：`go test ./...`（无 PG、无 race）；aero-id：`go test ./... -count=1 -race`（无 PG）；aero-im：`cargo test --workspace --lib` + PG 集成 job（唯一有 live-PG 的 CI）；console：Flutter test/ 113 文件 |
| **tagged（PG-gated）** | `AUDIT_TEST_POSTGRES_DSN` / `#[ignore = "requires live Postgres"]` / `DATABASE_URL` | sink 7 个 PG 测试（无 DSN 时 SKIP）；aero-im 30 个 audit `#[ignore]`（仅 aero-im CI 的 PG job 执行）；aero-vault/aero-id **无** PG-gated 测试 |
| **manual / chaos / bench / conformance** | e2e、drill、benchmark、discovery sweep | `test/e2e/fullstack.sh`（manual，dev token）；**无** D1 故障注入 drill；`BenchmarkIngest` 存在但无 envelope 记录；discovery sweep **不存在**（现有测试反而锁死错误契约） |

## 1. 本修订实际运行的命令（Test inventory & commands run）

| # | 仓库 @ HEAD | 命令 | 结果 |
|---|---|---|---|
| 1 | sink @ fb4fd97 | `go test ./...` | **PASS**（150 个 Test func） |
| 2 | sink | `python3 cli.py ci`（= make ci：vet + checks + go test + race + quality） | **PASS** |
| 3 | sink | `go test -race -count=1 ./internal/auth/ ./internal/httpapi/` | **PASS** |
| 4 | sink | `AUDIT_TEST_POSTGRES_DSN=... go test -race -count=1 ./internal/store/`（临时库 audit_test，migrations 001+004） | **PASS**（7 个 PG 测试，含乐观锁冲突、并发读 race）|
| 5 | IdP 部署仓 @ ac5d367 | `go test ./cmd/server/ ./token/jwt/` | **PASS**；`TestOIDCDiscovery` / `TestOIDCDiscoveryEndpoint` **PASS**（断言 `/authenticate`、`8080:0` 端口 bug —— 锁死错误契约）|
| 6 | IdP 部署仓 | `go test ./...` | **PASS**（1557 个测试） |
| 7 | SDK 仓 @ b3c839bb | `go test ./infrastructure/defaultimpl/` | **PASS** |
| 8 | aero-vault @ 35ff4ce | `go test ./internal/auditgovernance/... ./internal/config/...` | **PASS**（12 个测试） |
| 9 | aero-id @ 756b65b | `go test ./internal/connector/auditgovernance/... ./internal/audit/...` | **PASS**（7 个测试） |
| 10 | aero-im @ fbff3d5 | `cargo test -p aero-storage --lib -- audit`（默认套件） | **PASS**（7 passed；30 ignored） |
| 11 | aero-im | 新建临时库 + `aero-cli migrate`（238 个迁移全应用）→ `cargo test -p aero-storage --lib -- --ignored --test-threads=1 audit` | **PASS：30/30**（audit 组全绿；测试后已删临时库） |
| 12 | aero-im | 同一命令指向**陈旧本地库**（_sqlx_migrations 停在 176） | **18/22 FAILED**（`aero_effective_room_access` 等函数缺失）→ 纯 fixture 陈旧，非产品缺陷 |
| 13 | console @ f6d0d4e | 静态盘点 `test/`（113 文件）+ `audit_log_service.dart` | 审计相关测试仅覆盖**本地 ring**（lists/filters/clears）；无 sink 读 API 测试 |

未运行：sink `test/e2e/fullstack.sh`（需 docker compose verify 栈；静态审查确认使用
`dev:demo:tenant-auditor` / `dev:demo:compliance` dev token —— T-1.2 目标即替换它）；
任何性能/负载测试（无现成 harness，`BenchmarkIngest` 未记录 envelope）。

## 2. Requirement → Test 矩阵（状态 + 证据）

> 状态：🔴 RED（无测试且/或无实现）/ 🟡 PARTIAL（有代码或种子测试，断言不全）/ 🟢 GREEN（本修订实测通过）

| 验收断言 | 状态 | 现状证据（本修订实测） | 缺口 |
|---|---|---|---|
| **T-1.1** dev-token 401 + dev-only 配置拒绝 + manifest 扫描 | 🔴 | 单元种子 `TestDevAuthRejectsWithoutOptIn` PASS（auth_test.go:35，`Authenticator{}` 零值）；但 `main.go:45` 默认 `true`、`ValidateConfiguration`（verifier.go:45-47）在 `AllowDev=true` 时**放行 dev-only 信任源**、`deploy/docker-compose.verify.yml:12` 显式 `"true"` | 无路由级 401 sweep 测试；无 manifest 扫描脚本/测试；无 dev-only 启动拒绝测试 |
| **T-1.2** 跨仓 e2e（IdP token，dev off） | 🔴 | `fullstack.sh` 存在但全程 dev token；`scripts/mint-token.sh` 不存在；sink verifier 要求 kid（verifier.go:167），部署 IdP `generator.go` header 仅 `{alg,typ}`；`sso.go` ClientCredentials 铸 `{sub,aud,iat,exp,uid}` | 无 e2e 可跑（铸币侧缺 kid/claims）；无任何 CI 执行 e2e |
| **T-2** discovery sweep 全绿 + `token_endpoint=/token` | 🔴 | `TestOIDCDiscovery`（routes_test.go:41）断言 `/authenticate`、`/login`；`TestOIDCDiscoveryEndpoint`（:497）断言 `8080:0/login` —— **双测试 PASS，锁死缺陷**；routes.go:667 广告 `/authenticate`、:671 广告 `/revoke`（无路由 → 404）、:678 广告 3 种 auth method | sweep 测试不存在；现有 2 测试必须**重写**而非新增 |
| **T-3** vault 422 → 终态；dead 行不阻塞 | 🔴 | migration 0039 **无 status 列**（仅 delivered_at_ns）；`relay.go` `retryFact` 对任何非 202 一律有界退避重试（`classifyRelayError` 无永久类）；无 dead 概念 | 无测试；无实现 |
| **T-4** 确定性 event_id 再生成折叠 Duplicate | 🔴 | vault `facts.go` 3 处 `uuid.NewString()`；aero-id `audit_hold.go:59,74`、`legacy_cutover.go:88` 用 `idgen.NewID()`；sink 侧 dedup 单测存在且 PASS（`TestIngestIdempotencyConflictAndIntegrity`：202 Duplicate + 409 conflict） | 无"清除行→reconcile→Duplicate 回执→QueryEvents 恰好 1 行"测试；生产端无确定性 ID |
| **T-5** aero-id mock Record 失败可观测 | 🔴 | 8 个 `_ = s.audit.Record(...)` 实测在案（account.go:155,184、callback.go:160,221,247、audit_hold.go:59,74、legacy_cutover.go:88）；`ObserveAuditIngestFailure` 指标存在（pkg/metrics/control_plane.go:97）但 **0 个服务调用点**；无 mock 失败测试 | 无 T-5 测试；无 grep 门禁 |
| **T-6** PG 并发 ingest + readyz store probe | 🟡 | store 层乐观锁/并发读 race 测试**实测 PASS**（真 PG + `-race`）；`statusForError` 有 domain 映射；但 `ErrSnapshotConflict` 在 service 层**无处理**（裸 500 逃逸路径仍在）；readyz 只探 archive（`TestHTTPReadyzArchiveProbe` 实测 PASS，锁死现状）；**sink 无 CI workflow** | 无 HTTP 层并发 ingest 测试（2 并发等 digest → 1 行 + 202/Duplicate）；无停 PG → readyz 503 测试；无 CI job 承载 PG 测试 |
| **T-8(a)** token claims（kid/iss/aud/scope/client_id/tenant_id/roles） | 🔴 | SDK `buildAccessPayload`（ed25519Payload）无 tenant_id/roles 字段；部署 IdP 无 kid/scope 校验；无任何断言测试 | 无测试 |
| **T-8(b)(c)(e)** query 凭据→400、Content-Type 强制、cc 无 refresh_token + no-store | 🔴 | `token()` 用 `r.FormValue`（**query + body 都解析** → query 凭据当前会被接受）；无 no-store/refresh_token 省略逻辑；无测试 | 无测试 |
| **T-8(d)** 未注册 scope → 400 invalid_scope | 🔴 | `/token` handler 无 scope 解析/注册表校验（routes.go:247-300 实测）；无注册表 | 无测试 |
| **T-9** introspect 无凭据 → 401 | 🔴 | `TestIntrospect_MissingParams` 断言**400**（缺 token/client_id）；`introspect` handler 不认证调用方（V2-4 复核一致）；无凭据→401 测试不存在 | 无测试 |
| **T-10.1** aero-id 401 → 立即刷新 | 🔴 | vault `http.go:126` 有 401 失效（实测）；aero-id connector **无**；publisher_test 无 401 用例 | 无测试 |
| **T-10.2** dead → replay 重投递；purge 不静默 | 🔴 | aero-id 无 replay 工具；TTL `PurgeOlderThan` 静默删除（repository/audit.go:101）；vault 无 replay | 无测试 |
| **T-11** 无 relay 时 scope 配给 fail-closed | 🔴 | 无 scope registry（B4-2 未实现）→ 无法测试 | 无测试；无 registry |
| **T-12** read self-audit（查询后出现调用者行） | 🔴 | `getEvent`/`QueryEvents` 实测**不写** self-audit；`adminAction` 自审计仅 mutation 路径（`TestHTTPAdminActionsSelfAudit` PASS）；全仓无 `audit.event.read` 字符串 | 无测试 |
| **D1** sink 停 60min → vault readyz 200 + 450s 告警 | 🔴 | `Ready()`（runtime.go:145-161）在 maxLag 超限时**返回 error → 503**；无 degraded 状态；无告警；无 drill harness | 无测试；无实现 |
| **L0 持久化**（IdP stock audit memory→sqlite；10 logins → 10 行） | 🔴 | 部署 IdP audit backend 默认 memory（database F-02 复核一致）；无测试 | 无测试 |
| **P2 parity** / **L1 聚合** | 🔴 | aero-im：`message.send` 等 outbox 尚不存在（无 `audit_outbox` 迁移，实测 grep 零引用）；sink `BenchmarkIngest` 存在（bench_test.go:37）但无 envelope 记录 | 无 parity 检查测试 |
| **F-03 激活门**（空 bindings + enabled → boot 失败） | 🟡 | aero-id `validateAuditBindingCoverage` 路径已验证存在（revision-plan）；vault 同类 | 无跨仓首个事件 e2e |
| **37/37 aero-im audit 测试** | 🟢* | 默认 7 pass；PG-gated 30 个**实测 30/30 PASS**（全新迁移库）；aero-im CI 有 PG job 跑 `--ignored`（ci.yml:135-143） | *仅限全新迁移 fixture；本地陈旧库复测即红（见 F-3） |

**门禁可测性汇总**：G1（T-1.1/1.2/8a）🔴 三断言全无测试且铸币侧缺 kid/claims；
G2（T-6/T-12/治理 fail-closed/拓扑/容量）🟡→🔴（store 层有、HTTP 层与 read self-audit
无）；G3/G4（T-3/T-4/T-5/T-10/D1）🔴 全缺；G5（T-2/T-8/T-9/L0）🔴 且现有测试锁死
错误契约；G6（37/37、T-11、moderation）🟢（37/37 实测可绿）→🔴（T-11、优先级无）；
G7（T-12 联合）🔴；G8 依赖全部。

## 3. Findings（按严重度）

### F-1 【Critical】G1 信任路径端到端不可测：铸币侧缺 kid/claims，e2e 无执行载体
- **证据**：部署 IdP `token/jwt/generator.go` header 仅 `{alg,typ}`（实测）；`sso.go:894-960`
  ClientCredentials 铸 `{sub,aud,iat,exp,uid}`（实测）；sink `verifier.go:167`
  `remote JWKS token requires kid`（实测）；`fullstack.sh` 用 dev token（实测）；sink 无 CI。
- **未测失败**：kid 缺失 → JWKS 校验全拒；即便补 kid，缺 `tenant_id`/`roles` → sink
  tenant 一致性失败（design 说 422）。
- **回归风险**：B1-1（dev 关闭）先落地而 B4-1 未落地 → 生产流量全 401（设计如此，
  但 rollback 面是整条路径）。
- **要加的测试**：
  1. `TestClientCredentialsJWTHeaderAndClaims`（IdP 部署仓 `cmd/server/routes_test.go`）：
     `POST /token`（cc, `resource=audit-governance`）→ 200；JOSE header 断言 `kid != ""`；
     claims 断言 `iss`（= 配置 allowlist 项）、`scope` 含 `audit:event:write`、`client_id`、
     `tenant_id`、`roles`。**验收断言**：五断言全过；iss 非 Host 派生。
  2. `scripts/mint-token.sh` + `test/e2e/fullstack.sh` 改造：`AUDIT_ALLOW_DEV_AUTH=false`
     下 `POST /api/v1/events` → 202 `ledgered` → `GET /events` 200 → `POST /exports` 202。
     **验收断言**：e2e 无任何 `dev:` 前缀 token；三步骤状态码如断言。
  3. 批次归属测试：B4-1 的 `buildAccessPayload` 改动落在 **SDK 仓**
     （`/home/u1/workspace/demo/snaplink`，实测 `issue_payload.go` 仅存在于该树），
     部署仓只改 `sso.go`/`generator.go`/`routes.go`——建议在批次清单加"双仓断言"
     （两仓各有一个 claims 测试），防止实施者改错仓（V2-5 复述为测试门禁）。

### F-2 【Critical】S1 三件套全缺：默认 true、dev-only 放行、manifest 无扫描、无路由级 401 测试
- **证据**：`main.go:45` 默认 `true`（实测）；`verifier.go:45-47`：`AllowDev=true` 且无
  其他信任源时 `ValidateConfiguration` **返回 nil**（实测）；`docker-compose.verify.yml:12`
  `"true"`（实测）；唯一种子测试 `TestDevAuthRejectsWithoutOptIn` 只测 `Authenticator{}`
  零值（实测 PASS）。
- **要加的测试**：
  1. `TestDevToken401OnEveryRoute`（HTTP 层 sweep）：`AllowDev=false` server 上对
     ingest/query/export/integrity/legalhold/admin 全路由打 `dev:platform:platform-admin`
     → **全部 401**。验收断言：路由清单内无 2xx/403 逃逸。
  2. `TestValidateConfigurationRejectsDevOnlyTrust`：`Authenticator{AllowDev:true}` 无
     JWKS/公钥/secret → 启动报错。验收断言：err != nil 且消息含 trust source。
  3. `checks/test_manifest_dev_auth.py`：扫描 `deploy/` 全部 compose/helm，
     任何 `AUDIT_ALLOW_DEV_AUTH: "true"` → 失败。验收断言：扫描脚本退出码非 0 当且仅当
     存在 true。

### F-3 【High】aero-im "37/37" 依赖全新迁移 fixture；本地/陈旧库复测 18 失败（fixture 卫生）
- **证据**：全新迁移库上 `--ignored audit` **30/30 PASS**（本修订实测）；陈旧 `aero` 库
  （migration 176 vs 238）上 **18/22 FAIL**（实测，缺 `aero_effective_room_access` 等函数）。
- **风险**：任何人在未跑 `aero-cli migrate` 的本地环境复测 → 假红；CI 与本地环境漂移
  导致"本地绿/CI 红"或反之；B5-1 验收断言（37/37）在 CI 有承载（ci.yml PG job）但
  本地无文档化 fixture 步骤。
- **要加的测试/工具**：`scripts/test-fixture.sh`（create DB → `aero-cli migrate` →
  跑 audit 组 → drop DB），或 Makefile target；把批次清单 B5-1 验收改为
  `make audit-pg` 文档化命令。**验收断言**：`make audit-pg` 在空 PG 上 37/37 绿，
  重复两次（幂等）。

### F-4 【High】T-6 缺 HTTP 层并发测试 + readyz store probe 测试（现状被测试锁死）
- **证据**：store 层乐观锁/race 测试实测 PASS；`ErrSnapshotConflict` 在 service 层无
  处理（grep 实测零引用）；`TestHTTPReadyzArchiveProbe` 只探 archive（实测 PASS）；
  sink 无 CI workflow。
- **要加的测试**：
  1. `TestHTTPConcurrentIngestSameDigest`：2 goroutine 同 digest 并发 `POST /api/v1/events`
     → 恰好 1 行 ledger + 一 202 一 Duplicate（receipt 含 `duplicate:true`），**无 500**。
     验收断言：状态码集合 = {202,202} 且其中一个 `duplicate`，`QueryEvents` 1 行。
  2. `TestHTTPConcurrentIngestDiffDigest`：同 event_id 异 digest → 一 202 一 **409**。
  3. `TestReadyzStoreProbe`：`AUDIT_POSTGRES_DSN` 指向停摆 PG → `/readyz` **503**
     （今天 200）。验收断言：503 且 body 含 store。
  4. CI：sink 增加 `.github/workflows/ci.yml`（postgres service + `AUDIT_TEST_POSTGRES_DSN`
     跑 store/outbox PG 测试，不再 SKIP）。

### F-5 【High】T-12 read self-audit 无实现且无测试（F-06/F13 双缺）
- **证据**：`getEvent`（server.go:289-296 实测）只读不写；`QueryEvents`/export 同；
  全仓无 `audit.event.read` 字符串；mutation 侧 `TestHTTPAdminActionsSelfAudit` PASS。
- **要加的测试**：`TestQueryAppendsReadSelfAudit`：actor A 查询 → 同 store update 内出现
  `audit.event.read` 行（actor=A、query_filter/result_count/request_id 齐全）；export →
  `audit.event.export`。**验收断言**：查询响应 200 后立即查询自身即见该行；直接 API
  调用无法绕过（无开关可关）。
- **回归风险**：B6-1（console）以 T-12 为前置，T-12 缺 → B6 整批悬空。

### F-6 【High】vault F3/H1：无死信、Ready 翻 503、无 D1 drill、无指标
- **证据**：migration 0039 无 status 列（实测 SQL）；`retryFact` 无永久类（实测）；
  `Ready()` maxLag 超限返回 error（runtime.go:157 实测）；relay/runtime **0 处**
  Observe 调用（实测）。
- **要加的测试**：
  1. `TestPermanentFailureReachesDeadState`：注入 422 响应 → 一个投递周期内 status=3、
     `OldestPendingAuditGovernance` 排除该行、`Ready()==true`、批次其余行继续投递。
     验收断言：dead 行存在且 `Ready()` nil。
  2. `TestReadyzStays200WithBacklogAndPoison`：sink 不可达 + 1 条 poison 行 →
     `/readyz` 200（degraded 报告），**绝无 503**；450s（maxLag×0.5）处 alert 触发
     （可注入 clock）。
  3. D1 drill：`scripts/drill-sink-down.sh`（停 sink 60min 压缩为注入 clock）——作为
     manual 套件进入 `docs/snaplink-platform/v2/reviews/` 引用。验收断言：drill 结束
     readyz 恒 200；alert 恰好一次。

### F-7 【High】T-4 确定性 event_id 无测试、生产端全部随机 ID
- **证据**：vault `facts.go` 3 处 `uuid.NewString()`、aero-id 3 处 `idgen.NewID()`
  （实测）；sink dedup 单测 PASS 但只测"同事件重复提交"。
- **要加的测试**：`TestRegenerationReusesEventID`（vault 仓）：写入 fact → 删除 outbox
  行 → gap reconcile 再生成 → 断言 `event_id` 与首次一致；联合 sink 验证回执
  `duplicate:true`、`QueryEvents` 恰好 1 行。**验收断言**：两次 event_id 字节相等；
  sink 侧无第二行。
- **回归风险**：T-4 是 G3/G4 门禁；若 event_id 随机，sink dedup 对再生成是空转
  （DS-04 复核一致）。

### F-8 【Medium】IdP 现有 discovery 测试是"缺陷回归锁"——必须重写而非新增
- **证据**：`TestOIDCDiscovery`（断言 `/authenticate`、`/login`）与
  `TestOIDCDiscoveryEndpoint`（断言 `8080:0` 端口 bug）本修订实测双 PASS。
- **要加的测试**：`TestDiscoverySweepAdvertisedEndpoints`：解析 metadata → 对每个
  advertised endpoint 发真实请求 → 2xx/405 **绝不 404**；`token_endpoint == "/token"`；
  `authorization_endpoint` 在无 `/authorize` 时**不存在**；`revocation_endpoint` 实现或
  移除；`token_endpoint_auth_methods_supported == ["client_secret_post"]`（Basic 未实现
  前不广告）；issuer ∈ 配置 allowlist。**验收断言**：sweep 零 404；三条元数据等式成立。
- **回归风险**：若只新增 sweep 而保留旧测试，错误契约继续被 CI 锁死——批次 B4-3 必须
  显式**删除/重写** `routes_test.go:41` 与 `:497` 两测试（实施批次清单已写"重写"，
  本审查确认这是强制项）。

### F-9 【Medium】T-8(b)(c)(e)/T-9 无测试，且 `r.FormValue` 让 query 凭据被接受
- **证据**：`token()` handler 用 `r.FormValue`（query + body 均解析，实测）；introspect
  无调用方认证；cc 响应含 refresh_token 且无 no-store（代码走查）。
- **要加的测试**：
  1. `TestTokenRejectsQueryStringCredentials`：凭据放 query → **400**。
  2. `TestTokenRequiresFormContentType`：`Content-Type: application/json` → 400。
  3. `TestClientCredentialsResponseOmitsRefreshToken`：cc 响应无 `refresh_token` 键 +
  `Cache-Control: no-store`。
  4. `TestIntrospectRequiresCallerAuth`：无凭据 introspect → **401**（RFC 7662 §2.1）。
  **验收断言**：各状态码/头断言全过。

### F-10 【Medium】aero-id F5/F6/F8/F9 四测试全缺，代码实测均未实现
- **证据**：8 个 `_ =` 调用点（实测）；`Record` inline publish 存在（双投递）；
  connector 无 401 失效（grep 实测）；无 replay 工具、TTL purge 静默（实测）。
- **要加的测试**：`TestMockRecordFailureIsObservable`（mock repository `Record` 失败 →
  `ObserveAuditIngestFailure` counter +1 **或** 操作失败；验收断言：counter 断言非零）；
  `TestSingleDeliveryPath`（F6 drill：sink 注入 5s 延迟 → 请求 p99 不漂移，inline
  publish 删除后同一事件仅 1 条投递）；`Test401InvalidatesCachedToken`（T-10.1：AS
  revoke → 下一次 publish 立即刷新，token-source 调用计数断言，不等到 TTL）；
  `TestReplayRedeliversDeadRow`（T-10.2：dead → replay → 重投递成功入账）。

### F-11 【Medium】console B6：路径漂移 + 只测本地 ring
- **证据**：批次清单写 `lib/services/portal_api.dart`，实际为 `lib/api/portal_api.dart`
  （实测）；`AuditLogService` 为 1000 条 localStorage ring（实测代码）；
  `test/admin_support_tabs_test.dart` 只测 lists/filters/clears 本地条目。
- **要加的测试**：`test/audit_log_server_sourced_test.dart`：mock sink 读 API →
  审计页只渲染服务端记录；清空/伪造 localStorage 后页面证据不变（S9 场景）；
  查询携带 tenant_id + trace_id（经 BFF）。**验收断言**：伪造 localStorage 不改变
  页面记录集；请求头含 trace_id。

### F-12 【Low】批次清单与测试层的核对项
- B3-5（vault scope 对齐"无代码改动"）**复核成立**：`RequiredScope`/`token.go` 严格
  scope 检查实测在案，`TestTokenSourceRejectsExpandedScope` 实测 PASS（保留即绿）；
- B4-1 双仓归属（V2-5/DS-07 复述）已并入 F-1 测试 3；
- `audit:event:ingest`→`write`（F2）：三实现点均 enforce `write`（实测 grep），
  契约侧改名后无需 alias 测试，但建议加 grep 型注册表一致性检查
  （`rules --check` 风格，见 F-13 第 5 条）。

## 4. 优先场景清单（按批次顺序；happy / boundary / error / race / recovery）

| 批次 | 场景 | 路径 | 优先级 |
|---|---|---|---|
| B1-1 | dev-token 关闭后：合法 IdP JWT 全路由通过；`dev:` 全路由 401；dev-only 配置拒绝启动 | happy/error | P0 |
| B1-7 | 跨仓 e2e：mint → ingest 202 ledgered → read 200 → export 202（dev off） | happy | P0 |
| B1-2 | 双写者等 digest 并发 → 1 行 + 202/Duplicate；异 digest → 409；停 PG → readyz 503 | race/error | P0 |
| B1-2 | file backend 断电（无 fsync）→ 丢失窗口文档化或 fsync 后无静默回退 | recovery | P1 |
| B1-3 | `BenchmarkIngest`：events/sec + p99 vs ledger 大小曲线 → 容量 envelope 记录 | bench | P1 |
| B1-4/5 | 治理操作 append 失败 → 回滚（无部分 legal hold）；查询/导出后出现 self-audit 行 | error/happy | P0 |
| B1-6 | 孤儿 outbox relay / Kafka consumer 移除后 grep 无 enqueue | boundary | P1 |
| B2-1 | mock Record 失败 → counter 递增；无 `_ =` 调用点（grep 门禁） | error | P0 |
| B2-2 | sink 慢（5s）→ 请求 p99 不漂移；单投递路径（2× 负载消除） | performance/race | P1 |
| B2-3 | revoke 后立即刷新（计数断言）；dead → replay → 重投递；purge 暂停 | recovery | P1 |
| B2-4 | 再生成 → sink Duplicate、ledger 1 行 | boundary | P1 |
| B2-6/B3-6 | 空 bindings + enabled → boot 失败；首个事件到 sink | error/happy | P0 |
| B3-1 | 422 → 终态 ≤1 分类尝试；dead 行排除 lag；批次继续 | error | P0 |
| B3-2 | D1 drill：sink 停 60min → readyz 200 + 450s 告警一次；1 条 poison 不重启循环 | chaos/recovery | P0 |
| B3-4 | relay 指标 attempted/delivered/failed/dead/oldest-age 可喂 H2 | observability | P1 |
| B4-1 | cc token claims 全断言（kid/iss/aud/scope/client_id/tenant_id/roles） | contract | P0 |
| B4-2 | 未注册 scope → 400 invalid_scope；矩阵配给后无 403 | contract/error | P0 |
| B4-3 | discovery sweep：广告端点零 404；无 /authorize 不广告 code 流 | conformance | P1 |
| B4-4 | query 凭据 400；Content-Type 强制；cc 无 refresh_token + no-store；introspect 无凭据 401 | security | P0 |
| B4-5 | 重启 L0 不丢（10 logins → sqlite 10 行）；P2 parity；`auth.token.issue` 仅 L1 摘要 | recovery/boundary | P1 |
| B5-1 | 37/37 audit 测试（`make audit-pg`，全新 fixture）；P2 parity | happy/regression | P0 |
| B5-2 | 首个 `room.create` 到 sink；422 → 终态；T-11 fail-closed 拒绝 | happy/error | P0 |
| B5-3 | moderation 优先于积压（注入积压 drill） | boundary | P1 |
| B6-1/2 | 查询触发 self-audit；console login 事件（client_id=console）无重复 | happy/boundary | P1 |

## 5. CI/manual 套件缺口、flake 风险、所需 fixture、退出标准

### CI 缺口
1. **sink 无任何 CI workflow**——T-6 的 "Postgres-gated 测试进 CI 不再 SKIP" 无处承载；
   `fullstack.sh` e2e 无 CI；manifest 扫描无 CI。需新增 `.github/workflows/ci.yml`
   （postgres service + `AUDIT_TEST_POSTGRES_DSN` + manifest 扫描 step）。
2. aero-vault/aero-id CI 无 PG service、无 `-race`（vault）——T-3/T-5 的 PG 断言无处跑。
3. aero-im CI 是唯一 live-PG 模范（ci.yml PG job + migration 链 smoke + rolling-upgrade
   回归）——应作为其余各仓模板。

### Flake / fixture 风险
4. aero-im 本地 fixture 陈旧（176 vs 238）→ 复测假红（本修订实测 18/22 失败）；
   需要文档化 fixture 脚本（F-3）。
5. sink PG 测试共享单行 `audit_state_snapshot`，已正确禁止 `t.Parallel`，但 CI 与本地
   并行跑同一库会互踩——fixture 需独立数据库（`audit_test_<pid>` 或每个 job 新建）。
6. vault `Ready()` 翻转类测试依赖 clock——需注入 clock 避免真实 900s 等待（D1 压缩）。

### 所需 fixture（新增）
7. `scripts/mint-token.sh`（IdP cc token + claims 断言前置）；`test/e2e/fullstack.sh`
   dev-token 替换（B1-7）。
8. aero-im `make audit-pg`；vault/aero-id PG 测试库脚本；sink CI PG job。
9. 故障注入：D1 sink-down drill（clock 注入版）；F6 sink 延迟 drill（httptest 慢 handler）。

### 退出标准（per gate）
- **G1**：F-1 测试 1/2/3 + F-2 测试 1/2/3 全绿；`fullstack.sh` 无 `dev:` token；双仓
  claims 测试绿。
- **G2**：F-4 测试 1/2/3/4 + F-5 测试绿；拓扑 grep 无 enqueue；BenchmarkIngest 有
  envelope 记录。
- **G3**：F-10 四测试 + F-7 测试绿；grep 门禁（`_ = s.audit.Record` 零命中）进 CI。
- **G4**：F-6 测试 1/2/3 绿；`Ready()` 含 dead 行 = true；relay 指标存在。
- **G5**：F-8 sweep + F-9 四测试绿；**旧 `TestOIDCDiscovery`/`TestOIDCDiscoveryEndpoint`
  已删除或重写**（强制项）；L0 持久化测试绿。
- **G6**：`make audit-pg` 37/37（全新 fixture 实测基线）；T-11 fail-closed 测试；
  moderation 优先级 drill。
- **G7**：F-11 测试绿（console 只显示服务端记录）。
- **G8**：以上全绿 + sink/vault/aero-id CI 均含 PG job + DLQ/alert 上线验证。

### 结论
六个批次中 G1/G2/G3/G4/G5/G7 的验收断言**今天全部无法通过**（测试与实现双缺），
与 protocol/database/distributed 三份审查的"零 B4 项实现"结论一致；本审查新增的
可测性发现：① 唯一已"绿"的 G6 基线（aero-im 37/37）依赖全新迁移 fixture，本地
陈旧库假红（F-3）；② sink PG store 层是唯一已有真 PG 实测通过的底座（T-6 可从此
出发补 HTTP 层与 CI）；③ IdP 两处 discovery 测试是缺陷回归锁，B4-3 必须删除/重写
而非叠加（F-8）；④ 批次清单存在两处测试层引用漂移（`portal_api.dart` 路径、
`buildAccessPayload` 仓库归属），已给出双仓断言测试兜底。
