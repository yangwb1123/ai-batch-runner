全部五项原位修订完成，且未改任何代码。以下为修正清单：

---

# 修正清单 — v2 契约包 gate FAIL 五项（原位修订，无代码改动）

## ① B4 重钉（部署仓 vs legacy 树）＋ ② tenant_id 覆写 ＋ ③ F5 计数 ＋ ④ gRPC 拓扑 ＋ ⑤ B6-1/B4-3

### 文件 1：`docs/snaplink-platform/v2/implementation-gate.md`

| 小节 | 改前 → 改后 |
|---|---|
| 批次模型段 | ② "discovery 真值化"（F-3）在 IdP 仓库（`cmd/server/routes.go`）→ 在 IdP **部署仓**（`interfaces/sso/server_discovery.go`/`server_discovery_config.go`）；新增 gate 重审修正说明（B4 钉部署仓 SDK 树 `b3c839bb`、legacy 树 `ac5d367` 不引用、补 kid 已移除、F5=49、B1-8、gRPC） |
| snaplink 表 #1（B4-1） | 文件列 `token/jwt/generator.go:52-55`；`sso.go:894-960` → 部署仓路径（`issue_payload.go:26`、`shared/core/spi.go:171`、`server_token.go:17`、`server_token_clientauth.go:193`）；改动列 "header 补 **kid**" → "**kid 已存在**（`ed25519_issue.go:33` + 三 issuer kid 字段，SEC-V2-2 复核，不再补）"；scope 校验注明 per-client allowlist 已存在 |
| snaplink 表 #2（B4-2） | 文件列 `cmd/server/routes.go` token handler → `interfaces/sso/server_token.go:122`（scope 拆分）+ 新 scope 注册存储 |
| snaplink 表 #3（B4-3） | 文件列 `cmd/server/routes.go:63-77,663-684`；`routes_test.go:41` 重写 → 部署仓 `server_discovery.go` + `server_discovery_config.go:61,142` + `shared/core/consts.go:9,21-23`；改动列改为**删除/重写两个** legacy 缺陷回归锁测试（`TestOIDCDiscovery` @ legacy `routes_test.go:41` 断言 `/authenticate`、`/login`；`TestOIDCDiscoveryEndpoint` @ legacy `routes_test.go:497` 断言 `8080:0` 端口 bug），不得带入部署仓；部署仓 `test/oidc_discovery_test.go`/`rootcov_discovery_test.go` 补 sweep |
| snaplink 表 #4（B4-4） | 文件列 `cmd/server/routes.go:247-300`；`sso.go:909`；`/introspect` → 部署仓（`server_token.go:17`、`server_token_clientauth.go:193,239`、`server_resource.go:255-257`、`token_client_credentials.go:21`、`oauthwire/bind.go:28`）；改动列标注**三项已存在**（仅 body 解析 / introspection 调用方认证 / cc 省略 refresh_token+no-store），剩余 Content-Type 强制 + constant-time |
| snaplink 表 #5（B4-5） | 文件列 `cmd/` 新 wiring + `config_audit.go` → 部署仓已有基建（`infrastructure/auditgovernance/`、`platform/audit/`、`cmd/snaplink-audit-provisioner/`），任务改为"接线/复用" |
| aero-id 表 #1（B2-1） | 文件列 "（8 点）" → "（8 个 governance 调用点列明；全仓 **49 个 `_ =` 站点**，SEC-V2-7 实测 @ `756b65b`）"；改动列 grep 门禁覆盖 49 站点非仅 8 点 |
| sink 表 #6（B1-6） | 拓扑收敛：新增 `internal/grpcapi/server.go`；**gRPC ingest（`--grpc-listen`）声明为受支持入站**（同一认证/校验/self-audit/tenant 一致性约束，DS-09）；验收改"契约声明覆盖全部活动入站" |
| sink 表 **新增 #8（B1-8）** | — → tenant 一致性强制（S2/DS-08/SRE-07）：envelope `tenant_id` ≠ token claim → **422 拒绝入账**；**禁止覆写**（`service.go:389` 静默重标）；验收 = T-13 |
| console 表 #1（B6-1） | `lib/services/portal_api.dart` → `lib/api/portal_api.dart`（实际路径，QA F-11 实测；非 `lib/services/`） |
| 门禁汇总 G2 | B1-2..6 → B1-2..6 + B1-8；通过条件加 T-13（tenant 422） |

### 文件 2：`docs/snaplink-platform/v2/implementation-batches.md`（与上完全同步）

| 小节 | 改前 → 改后 |
|---|---|
| 归属校正注 + 新增 gate 重审修正注 | 同文件 1（部署仓路径、kid 已存在、F5=49、B1-8、gRPC） |
| snaplink 表 #1–#5 | 与文件 1 同款重钉（含 kid 已存在标注、已存在项三项、治理连接器复用已有 `managed_relay.go`） |
| aero-id 表 #1 | "（8 个 post-commit 调用点）" → "（8 个 governance 调用点列明；全仓 **49 个 `_ =` 站点**，SEC-V2-7 实测 @ `756b65b`）"，grep 门禁覆盖全量 |
| sink 表 #6 / 新增 #8 / console #1 / G2 | 同文件 1 |

### 文件 3：`docs/snaplink-platform/v2/integration-design-v2.md`（接入设计摘要）

| 小节 | 改前 → 改后 |
|---|---|
| ② F5 落实情况 | "**8 个 post-commit `_ = Record(...)` 静默丢失点**" → "**49 个 `_ =` 站点**（SEC-V2-7 实测 grep `internal/`+`service/`+`cmd/` @ `756b65b`；8 个 governance 点列明），grep 门禁覆盖全量 49 站点" |
| ⑤ 覆盖清单 | sink "（7 项 P0：…）" → "（8 项 P0：…/tenant 一致性 422）" |
| **新增「重审修正（gate FAIL 五项）」节** | — → ① DS-08/SRE-07 明确任务：sink 禁覆写 envelope `tenant_id`、冲突 **422 拒绝入账**（已入 sink 清单 T-13 + B1-8）；② DS-09：gRPC 入站声明为受支持拓扑（同一契约约束，B1-6 同步） |

### 文件 4：`docs/snaplink-platform/v2/audit-ingestion-design-v2.md`（接入设计契约本体）

| 小节 | 改前 → 改后 |
|---|---|
| §0.0 provenance 表（claim 契约行） | 溯源 "security S2, protocol F-2" → "security **SEC-V2-2（部署仓 SDK 树实测）**；protocol F-2（legacy 树证据，已被 SEC-V2-2 取代）" |
| §0.0 provenance 表（aero-id 行） | "8 post-commit `_ = Record(...)` discard sites" → "**49 个 `_ =` 站点**（SEC-V2-7 实测 @ `756b65b`；8 个 governance 点列明）" |
| §0.1 修订日志 F5 行 | "in-tx vs 8 post-commit sites" → "in-tx vs **49 个 `_ =` 站点**（SEC-V2-7）" |
| §0.1 修订日志 新增「重审（gate FAIL）」行 | — → DS-08 禁覆写 422（T-13/B1-8）、DS-09 gRPC 拓扑、F5 计数 8→49、B4 重钉（kid 已存在）、B6-1 路径 |
| snaplink 实现前置任务清单 | 4 行全部重钉：claim 行（kid 已存在、部署仓路径）、scope 行（`server_token.go:122`）、discovery 行（**两个 legacy 测试删除/重写** + 部署仓测试补 sweep）、治理连接器行（复用 `managed_relay.go` 等） |
| console 实现前置任务清单 | `+ portal_api.dart` → `+ lib/api/portal_api.dart`（实际路径，QA F-11；非 `lib/services/`） |
| sink § Supported ingest topology | "HTTP ingest API + self-audit writer" → "HTTP + **gRPC ingest（`--grpc-listen` 注册 `internal/grpcapi/server.go`，活动入站 DS-09，同一认证/校验/self-audit/tenant 约束）** + self-audit writer" |
| sink § Ingest API contract（Tenant consistency 行） | 追加 **"The sink MUST NOT overwrite the envelope `tenant_id`…（`service.go:389` 静默重标，DS-08/SRE-07）；conflict → 422 reject, zero rows ledgered"** |
| sink 实现前置任务清单 | 拓扑收敛行加 gRPC（`internal/grpcapi/server.go`）；**新增「tenant 一致性强制（禁覆写）」行**（`service.go:389`，验收 T-13） |
| aero-id § 路径清单表后 | 新增 **Scope note**：全仓 49 个 `_ =` audit 触达行（含 `audit.Log` 通用通道），F5 批次覆盖全部、非仅 8 点（SEC-V2-7） |
| aero-id 实现前置任务清单 #1 | "8 个 post-commit 调用点…" → "**49 个 `_ =` 站点**（SEC-V2-7 实测；8 个 governance 点列明），grep 门禁覆盖全量" |

### 关于指令 ③ 的文件名说明
指令写 `audit-events-v2.md`，但该文件（注册表修订摘要）**本无 F5 计数**——F5 计数实际位于 `integration-design-v2.md`（接入设计摘要）与 `audit-ingestion-design-v2.md`（契约本体）以及两份实施清单中，已全部原位修正为 49（含 provenance 同步）。`audit-events-v2.md` 本身无需改动。审查证据文件（reviews/）、`revision-plan.md`、`revision-gate.md` 作为历史记录保持原样。

---

**证据核对（修订依据，全部实测）**：部署仓 = `/home/u1/workspace/demo/snaplink` @ `b3c839bb`（`kid` 已 stamp 于 `ed25519_issue.go:33`；`token/jwt/generator.go`/`sso.go`/`cmd/server/routes.go` 均不存在）；legacy 树 = `github.com/opensso/sso` @ `ac5d367`（`routes_test.go:41`/`:497` 锁死 `/authenticate`、`8080:0` 缺陷）；aero-id @ `756b65b` grep 实测 **49** 个 `_ =` audit 触达行；sink gRPC 入站 = `cmd/audit-api/main.go` `--grpc-listen` → `internal/grpcapi/server.go`；sink 覆写点 = `internal/service/service.go:389`；console 实际路径 = `lib/api/portal_api.dart`。
