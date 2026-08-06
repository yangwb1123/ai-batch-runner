五项修正已逐项独立复核（读修订后产物 + 对部署仓/sink/aero-id 代码实测），全部落实：

1. **B4 重钉** ✓ — 13 个引用的部署仓文件全部实测存在（`b3c839bb` 树）；四份契约/清单文档中 `token/jwt/generator.go`、`sso.go:894/909`、`cmd/server/routes.go` 零残留；"补 kid" 改动项已移除并标注"kid 已存在"（`ed25519_issue.go:33` 实测）；B4-4 另标注三项已存在（仅 body 解析/introspect 认证/cc 无 refresh_token）；provenance 改为 SEC-V2-2 部署仓证据。
2. **tenant_id 覆写 422** ✓ — 契约 sink 节新增 "MUST NOT overwrite…conflict → 422 reject, zero rows ledgered"（覆写点 `service.go:389` 实测在案）；sink 任务清单新增「tenant 一致性强制（禁覆写）」（T-13）；integration-design-v2.md 重审修正 §1；implementation-gate/batches 增列 B1-8，G2 门禁含 T-13。
3. **F5 = 49** ✓ — 契约 provenance 表、修订日志、Scope note、任务清单四处全部 49（独立 grep 实测 aero-id @ `756b65b` 恰为 49 个 `_ =` audit 触达行）；8 个 governance 点作为子集保留列明；契约/清单中旧计数零残留。
4. **F13 gRPC 拓扑** ✓ — 拓扑声明覆盖 gRPC（`--grpc-listen` → `grpcapi.Register` 实测 `main.go:50,124`，`internal/grpcapi/server.go` 存在，同一契约约束）；拓扑收敛任务、B1-6、修订日志、重审修正 §2 同步。
5. **B6-1/B4-3** ✓ — `lib/api/portal_api.dart`（标注非 `lib/services/`）在三处修正；B4-3 双测试 `TestOIDCDiscovery`（legacy `routes_test.go:41`，`/authenticate`）与 `TestOIDCDiscoveryEndpoint`（legacy `routes_test.go:497`，`8080:0`）在三处均列明，含断言、不得带入部署仓、部署仓测试补 sweep。

VERDICT: PASS - v2 修正完成，可进入人工批准
