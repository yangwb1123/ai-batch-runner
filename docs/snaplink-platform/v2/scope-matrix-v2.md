修订完成。完整修订版 scope 矩阵已写入 **`docs/snaplink-platform/v2/scope-matrix-v2.md`**（22KB），v1 的 `scope-matrix-draft.md` 未改动（历史文档保留）。机械校验通过：`audit:event:ingest` 仅剩 4 处，全部位于修订日志/删除说明（不再作为可用 scope 名）；`audit:event:write` 成为唯一运营名（14 处）。

## 三项强制修订的落实情况

**1. F2 — 全矩阵统一 `audit:event:write`，删除 `audit:event:ingest`**
- 六处修改点全部落地：§0.1 seed 名（`SCENARIO_ANALYSIS` P0-3 引用行）、§0.2 约定示例、**Flow 3 两行 scope**（aero-vault + aero-im）、Flow 4 "Must NOT grant to console" deny 行、cross-cutting rule 5（`aero-vault: audit:event:write only`）。
- Flow 3 新增说明块：scope 名**从实现冻结**（sink `require()` / aero-id `validOAuthBinding` / vault `RequiredScope`），并记录 v1 按旧名配给会在 rollout 时 403 的教训。
- 补 §0.2 命名空间注释：scope `audit:event:*`（冒号）≠ 事件类型 `audit.event.*`（点），与注册表 v2 §1.1 互引，防再次漂移。

**2. 每个 scope 行新增"实现强制点"列**
- 图例区分两类：**Verified** = 该 scope 名今日已在代码中 enforce；**Target** = 目标强制点（scope 本身 `[PROPOSED]`，今日无强制）。
- Flow 3 `audit:event:write`（aero-vault 行）为全矩阵唯一 **Verified**：sink `internal/httpapi/server.go` `require("audit:event:write")`、aero-vault `internal/auditgovernance/model.go` `RequiredScope` + `token.go` 严格 scope 检查；aero-im 行标 **Target**（同一三处强制点，待 relay 落地）。
- Flow 2 `snaplink:token:validate` 强制点标注了已核实缺陷：`POST /introspect` 路由存在但**调用方未认证**（协议 F-5），scope 准入在修复前不可执行。
- Flow 4 全部 12 个 admin scope 标 **Target**，注明机制现状（如 sink `auth.go` role→permission 映射已验证、BFF scope 准入 `[PROPOSED]`、S1 dev-auth 是当前唯一路径）。

**3. 最小权限形状完整保留**
- 4 组 "Must NOT grant" deny 列表、6 条 cross-cutting deny 规则、client_id 绑定、无通配符——逐条保留（仅名字修正与 S2 引用更新）。

**顺带的一致性修订（v2 标记，20 处）**
- Flow 2 新增 **Access-token claim 契约**（kid/iss/aud/scope/client_id/azp/tenant_id/roles 八项，含每项要求）——兑现注册表 v2 §0.2 锚点对 "scope-matrix-v2 Flow 2" 的引用（S2）。
- Flow 3 tenant-consistency 行改为引用 token `tenant_id` claim；Flow 4 绑定规则 1 注明 OIDC auth-code flow 本身 `[PROPOSED]`（F-3，无 /authorize）。
- §0.3 修订日志表；新增 open decision #7：每个 Target 强制点须在首次生产配给前按证据标准对代码验证，Flow 3 `audit:event:write` 是今日唯一可预注册名。
