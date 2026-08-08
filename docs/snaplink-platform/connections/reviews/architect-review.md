# 架构对抗审查：6 项目连接分析（architect）

> 角色：senior architect。输入：阶段 3 七个分项目文档
> （`docs/snaplink-platform/connections/{snaplink,aero-id,aero-vault,aero-im,audit-governance,snaplink-console,sverpweb}.md`）、
> kickoff、`AGENTS.md`（snaplink）、`docs/architecture/DIRECTORY_MAP.md`。
> 本审查只做分析与建议，不修改代码、不批准发布。

## 0. 本次审查实际执行的检查（证据边界）

对 `/home/dwp/snaplink`、`/home/dwp/aero-id`、`/home/dwp/aero-vault`、
`/home/dwp/snaplink-audit-governance` 做了针对性核查（grep + 读源码）：

- **Verified**：`protocols/oauth/oauthvalidate/scope.go:50` 规则②
  "Empty AllowedScopes ⇒ UNRESTRICTED"（C-1 的依据成立）。
- **Verified**：snaplink `proto/` 下无任何 `Validate` 服务
  （`grep -rn "service .*Validate" proto/` 空；只有 admin/audit/authz/
  discovery/netpolicy）。
- **Verified**：aero-id `api/proto/sso/v1/sso.proto` 定义
  `service SSO { rpc ValidateToken(...); rpc GetUsers(...); }`——**客户端
  依赖的服务在 snaplink 侧不存在**。
- **Verified**：aero-vault `internal/api/rest/admin.go:456` "caller is
  implicitly admin (mirrors the no-auth MVP behaviour)"；
  `admin_audit_test.go:30` `auth.Parse("") // disabled → implicitly admin`。
- **Verified**：aero-vault `internal/service/presign.go` `PresignGet`/
  `PreparePresignGet` 均经 `objectForAction(ActionDownload)`——**签发时刻
  有 ACL 检查**；残余缺口在签发后撤销窗口（见 F13）。
- **Verified**：snaplink `interfaces/admin/` 存在 `recordAdminConnectionAction`
  /`recordAdminUserAction`/`recordAdminTenantExport` 等 helper，但
  `internal/handler/` 中对 branding/backup/netpolicy/dr-mode/access-policy
  converge/admin token revoke 无 `Record(` 调用（F6 的"疑似缺口"方向成立，
  逐端点清单仍需代码走查确认）。
- sverpweb 树本次未独立复查（`/home/dwp` 下未见该目录），其证据以
  sverpweb.md 的阶段 3 核查为准，标 [doc]。

## 1. Scope、假设与已验证架构摘要

### 1.1 Scope

评审对象：snaplink（OIDC/OAuth2 身份签发方）、aero-id（账户/自服务）、
aero-vault（文件/ACL/部门）、aero-im（消息）、snaplink-audit-governance
（审计账本）、snaplink-console（管理控制面）、sverpweb（ERP 接入方）——
七份文档声明的接口清单、依赖方向、"为完成连接要做的改变"及克制清单。
不含：单项目内部功能评审（其他流水线覆盖）、部署拓扑之外的容量规划、
合规认证范围。

### 1.2 假设

1. 各文档"Verified"标注 = 阶段 3 在对应仓库代码级核查结果（本审查抽查
   一致，见 §0）。
2. "≤1s 校验延迟"需求未在证据中看到出处，按**未验证需求**处理（F7 决策
   依赖它）。
3. 生产拓扑以 `docs/snaplink-platform/DEPLOYMENT_192.168.123.52.md` 为
   准；本审查不假设具体灰度窗口与排班。

### 1.3 已验证架构摘要（跨文档一致图景）

- **认证织网**：snaplink 是唯一身份签发方（client_credentials 服务账号：
  `sso-admin`/`sverp-audit`/`sverp-vault`/`aero-im-vault`）；下游验签 =
  远程 JWKS（vault、governance、aero-id 备选），**例外：aero-im 自签发
  JWT（issuer "aero-im"），是第三签发方**。
- **授权织网**：snaplink `Authorizer.Check` 被 aero-id 复用；governance 把
  token scope 字符串**直接并入权限、无通配符展开**；vault `requireRESTScope`
  /`requireAdmin`（注册表关闭时隐式放行，MVP 行为残留）。
- **审计织网**：governance 账本是跨服务审计汇聚点——sverpweb **已全量
  对接**（338 事件 published、outbox+receipt 幂等）；vault 已实现默认关闭
  （事务内 fact + 租约投递）；aero-id 已实现默认关闭（**同步直报**
  `?wait_for=ledgered`，与目标态 outbox 语义不符）；aero-im **无
  connector**（outbox 基建可复用）。snaplink 自身**不向 governance
  上报**（无 connector，`auth.token.issue` 保持 L1）——与"两套账本分离
  是正确设计"（audit-governance.md C-4）互相印证。
- **控制面现状**：console 浏览器持 admin OIDC token 原样透传 nginx →
  snaplink（命中 `console-browser-bypasses-service-api` 禁止项）；governance
  账本对 console **完全不可达**（零 upstream）；console mutation 只落
  localStorage 日志。
- **依赖方向**：七文档无反向依赖证据；snaplink 出站强制依赖为零（全部
  opt-in：SCIM push/SSF/SIEM）；组合关系单向（sverpweb/console 为消费方）。
- **跨文档一致结论**（对抗审查的基准）：gRPC 校验推荐本地 JWKS、
  审计读写账号分离、schema 审批链、share-link deferred、双 token BFF
  前置依赖链 C-1→C-4→连接 8。

## 2. 发现表（按严重度排序）

| # | 严重度 | 发现 | 证据 | 影响 | 建议 |
|---|---|---|---|---|---|
| F1 | **High** | snaplink 无 scope 注册表：`AllowedScopes` 为空的 client **完全不设限** | `protocols/oauth/oauthvalidate/scope.go:50`（Verified，规则②）；snaplink.md C-1 | 新建/误配 client 静默获得全量 scope 签发权；scope 门禁形同虚设的静默默认 | 注册表 fail-closed：空 allowlist = 最小默认集（或拒绝签发），client 创建/更新校验门；C-1 是后续一切的前置 |
| F2 | **Critical** | console 浏览器持 admin OIDC token 直连 snaplink admin API | snaplink-console.md A（nginx.conf 全段代理、lib/ 核查）；命中平台禁止项 `console-browser-bypasses-service-api` | 浏览器 token 被窃（console 任意 XSS）= **全平台管理权限**；localStorage 日志可清除可伪造，无服务端审计 | 两 token 模型（B-1）：BFF 服务账号持枚举 admin scope、nginx 强制剥离浏览器 token、decision_id 契约桥保留用户归因；禁通配符 scope |
| F3 | **High** | sverpweb 审计读账号权限放大：`audit:event:read audit:platform:cross_tenant` + 代码硬编码 tenantId | sverpweb.md B-3；audit-governance.md C-1（[doc]） | 嵌入 ERP 的凭据持有跨租户读豁免；硬编码 tenant 使租户隔离形同虚设 | 新增租户限定 `sverp-audit-reader`（仅 `audit:event:read`），governance 侧建角色映射；对偶 deny 规则 |
| F4 | **High** | 部门同步**只增不减**（无差集 DELETE）+ sverpweb 仅启动时同步一次 | aero-vault.md C-2；sverpweb.md B-4②（[doc]） | 调岗/离职后成员残留 vault ACL；运行中组织变更无界滞留窗口 | `syncDepartment` 补差集 DELETE；启动同步之外增加周期/事件触发重同步；集成测试覆盖移除场景 |
| F5 | **High** | 撤销/删除不级联 vault ACL（sverpweb 本地 file_shares 投影与 vault ACL 双源） | sverpweb.md B-4①（[doc]） | 共享撤销后 vault 侧授权残留，越权读窗口不确定 | 级联语义或显式"按投影 + 过期"语义成文；四路径授权语义表 + 回归测试 |
| F6 | **High** | snaplink admin HTTP 面审计缺口：branding PUT/DELETE、backup、admin token revoke、dr/mode、access-policy converge、netpolicy 未见 Record 调用 | 本审查 grep（interfaces/admin 有 helper，internal/handler 无对应 Record；Verified 方向，逐端点待走查）；snaplink.md C-2 | admin 变更绕过服务端审计账本；gRPC 面 interceptor 自动覆盖形成不对称 | 逐端点补 `recordAdmin*` 调用 + 新事件类型进 `auditreport` 分类；加"admin 路由 × 审计事件"枚举测试 |
| F7 | **High** | aero-id 默认 gRPC 校验路径是**死依赖**：客户端带全套韧性栈指向不存在的服务 | `api/proto/sso/v1/sso.proto`（Verified）vs snaplink `proto/` 无 Validate 服务（Verified）；aero-id.md B | 默认配置下 gRPC 认证运行期必失败；JWKS 切换后语义变化（本地验签看不到撤销）无决策记录 | 定案 C-4：主推本地 JWKS（零新服务）；如 ≤1s 需求真实存在再评估 gated Validate 服务（claim 最小化+限流）；mTLS 维持否决 |
| F8 | **High** | governance schema 注册无审批链，立即生效 | audit-governance.md A（摄入 fail-closed 直接引用 Active schema；[doc]） | 任一 `audit:policy:write` 者可单方注册放宽入账面校验 | draft→approved 状态机 + 第二人批准（复用 restores 双人语义）；摄入只接受 approved |
| F9 | **High** | vault `requireAdmin` 在注册表关闭时隐式放行 | `internal/api/rest/admin.go:456`、`admin_audit_test.go:30`（Verified） | 生产误配 = admin 面无鉴权；`sverp-vault` 持整段 admin scope 仅为部门同步 | 生产 profile 配置校验 fail-fast（禁隐式放行）；拆 `vault:dept:manage` 独立 scope 或迁 console BFF |
| F10 | **Medium** | scope 命名漂移：实现名 `audit:event:write`/`audit:legal_hold:manage` ≠ 提案名 `audit:event:ingest`/`audit:legalhold:apply` | aero-vault.md B、aero-id.md B、audit-governance.md C-3（[doc]） | governance 精确匹配 scope 字符串，命名漂移 = 授予/拒绝静默错位 | 启用审计连接**前**定名（建议保留现状实现名，零改动）；scope-matrix 修订 |
| F11 | **Medium** | platform-map 组织模型归属与代码事实不符：aero-id 无 organization/department，sverpweb 组织目录实际在 vault | aero-id.md B/C-3（[doc]） | 所有权文档误导后续模块归属决策；风险：在 aero-id 重复落地组织模型 | 修正 platform-map（organization 归 vault）；aero-id 克制不落地（零重复） |
| F12 | **Medium** | aero-im 是自签发第三签发方（RS256 自持密钥），连接矩阵误列其为"JWKS 验签方" | aero-im.md B（[doc]） | 信任边界文档错误；若 im token 被其他服务接受将出现无法统一撤销的凭据面 | 修正矩阵；明确 im token 永不被他方接受（边界成文）；snaplink 关联维持默认关的可选 OIDC |
| F13 | **Medium** | presign get 撤销窗口：vault 签发时刻有 `ActionDownload` 检查（Verified），但直链 URL 在其有效期内不再受 sverpweb 本地 canRead 投影约束 | `internal/service/presign.go`（Verified）；sverpweb.md B-2（[doc]） | 本地可见性撤销后，已签发直链在 expires 内仍可下载；与受控引用双重校验不对称 | 直链默认短过期；受控引用 `/api/resources/:key` 为默认路径；不对称语义成文 + 回归测试 |
| F14 | **Medium** | vault 部门管理四个写端点无审计（与 admin.go 不对称）；批量删除路径审计待核查 | aero-vault.md C-3（[doc]） | 部门变更（权限变动根源）不在服务端审计内 | 补 `RecordAudit`；批量删除路径走查后补测 |
| F15 | **Medium** | aero-id 审计**同步直报**（`?wait_for=ledgered`）与 integration-design 的 outbox 目标态语义不符 | aero-id.md B/C-2（[doc]） | governance 故障时 aero-id 写路径被同步拖住或丢失直报窗口；目标态漂移 | 短期维持直报（本地账本幂等兜底、事件量小）+ 人工重放 runbook + DLQ 等价告警；规模化/故障频发时迁 outbox（aero-im 直接按 outbox 实现，模式已两次落地可复用） |
| F16 | **Low** | JWKS 轮换 grace 契约未跨服务成文（vault 刷新默认 3600s；snaplink 侧"旧 key 验证到 token TTL"语义已有） | snaplink.md C-3（[doc]）；snaplink AGENTS.md §3 | 轮换时下游可能出现窗口性验签失败；compromise 场景无演练 | grace ≥ 2× 最慢下游刷新间隔成文；compromise 无 overlap 路径 + 通知流程演练 |
| F17 | **Low** | console mutation 审计 = localStorage 日志（可清除可伪造） | snaplink-console.md A（[doc]） | 误把本地日志当审计证据 | 降级为 UX 辅助，明确非证据；服务端审计（BFF 透传 decision_id）为唯一事实源 |
| I1 | Info | 双账本分离（snaplink 本地 vs governance）设计正确：域/权威/保留均不同；缺口在统一时间线视图 | audit-governance.md C-4（[doc]） | — | console 经 BFF 聚合双源 + request_id/trace_id 关联；**不做跨账本同步** |
| I2 | Info | snaplink 出站强制依赖为零；不向 governance 上报是**已确认**现状 | snaplink.md B（[doc]） | — | 维持克制清单（不新增上报、不引入 OPA） |

## 3. 决策选项与权衡

### D1 gRPC token 校验定案（F7，前置依赖链第二环）

| 选项 | 权衡 | 结论 |
|---|---|---|
| A. 本地 JWKS（已实现） | 零服务端改动、故障隔离好；**看不到撤销**（验证窗口=token TTL）；需把 `jwks_url` 写入各服务配置为强制项 | **首选**。缓解：服务账号 token 缩短 TTL + client secret 轮换作为撤销等价手段；文档化撤回语义 |
| B. snaplink 新增 `snaplink.token.v1.Validate` | 满足 ≤1s/即时撤销强需求；新服务面 + 契约归属定案（snaplink vs aero-id）+ claim 最小化 + 限流 + 高可用要求 | 仅当 ≤1s 需求被确认为真实需求时评估；gated 特性；**不默认建设** |
| C. mTLS | 撤销即时、无 token 面；部署复杂度高、已被项目否决 | 维持否决 |

附带动作：aero-id 侧死路径（默认 gRPC 校验）必须显式退役或强制配置
`jwks_url`，禁止"默认配置指向不存在的服务"悬置。

### D2 审计连接启用顺序（F10→F15→F8）

1. 定名（保留 `audit:event:write` 实现名，修订 scope-matrix）；
2. 读写账号分离（F3，`sverp-audit-reader`）；
3. 启用 vault/id connector（幂等测试先行）；
4. schema 审批链（F8）在 governance 写面开放给 console/BFF **之前**落地；
5. aero-im outbox relay（其 `audit:event:ingest` 授予以 relay 为条件）。

### D3 console 两 token BFF（F2，依赖 C-1 完成）

| 选项 | 权衡 | 结论 |
|---|---|---|
| A. BFF 服务账号 + 四元组路由注册表 + decision_id | 浏览器 token 降级为仅用户会话；服务账号 scope 可枚举审计；governance/vault 可达；成本：nginx 改造 + BFF 新面 + 上游 actor 归因透传 | **首选**（B-1/B-2/B-3 合并执行，按需增长：首批 vault 部门 + governance 只读） |
| B. 维持直连 + 加固 | 短期成本低；不解决"浏览器 token=全平台管理员"结构问题，且命中禁止项 | 仅作为中间态（≤1 里程碑内替换） |
| C. 升级浏览器 token | 违反最小权限，被克制清单明确排除 | 否决 |

### D4 vault admin scope（F9）

| 选项 | 权衡 | 结论 |
|---|---|---|
| A. 拆 `vault:dept:manage` scope | 最小改动、立即消除整段 admin 持有；vault 侧加 scope→端点映射 | **首选**（C-5） |
| B. 迁 console BFF | 结构更优但与 D3 绑定、周期长 | D3 落地后的演进项，不阻塞 |
| C. 保持现状 | 生产禁隐式 admin 放行前提下仍持整段 admin | 否决（违背最小权限） |

## 4. 分优先级实施序列

### M0 配置与安全止血（并行，全部 fail-fast 可验收）

1. vault 生产 profile：注册表必须启用，否则启动失败（F9）；
2. sverpweb 审计读账号换 `sverp-audit-reader`（F3，含 governance 角色映射）；
3. gRPC 校验定案：全服务配置 `jwks_url`，aero-id 死路径退役（D1）；
4. 部门同步差集 DELETE + 周期重同步（F4）。

### M1 授权与审计完整性（依赖 M0）

5. scope 注册表 fail-closed（F1）——client 创建/更新校验门 + 空 allowlist
   语义变更；
6. snaplink admin HTTP 审计缺口闭合（F6）+ 路由×事件枚举测试；
7. 撤销/删除级联 vault ACL 语义成文 + 回归测试（F5）；
8. vault 部门管理写端点审计补齐（F14）。

### M2 控制面重构（依赖 M1.5 scope 注册表）

9. console 两 token BFF（D3）：nginx 剥离浏览器 token → BFF 服务账号 →
   四元组路由注册表（首批 vault 部门 + governance 只读）→ decision_id
   透传 → 双源时间线聚合；
10. governance schema 审批链（F8）在 BFF 写面开放前落地；
11. vault 拆 `vault:dept:manage` scope（D4-A）。

### M3 韧性契约与消息面

12. JWKS 轮换 grace 契约 + compromise 演练（F16）；
13. aero-im outbox relay + 端到端验收（im 删除 → 账本 → console 时间线）；
14. 审计直报 runbook + DLQ 告警（F15 短期项）。

### 兼容性计划

- 所有改动**追加式**：新 reader 账号=新增角色映射；BFF 路由=白名单新增；
  scope 名保留现状名避免破坏已授予凭据；presign 行为对合法路径不变。
- 破坏性变更仅一处：scope 注册表空 allowlist 语义（F1）——需先盘点
  现有 client 的 `AllowedScopes` 并全部显式填充后再切换 fail-closed。
- 每步完成即补契约文档（openapi/config-reference/error-codes，snaplink
  AGENTS.md §5 要求）。

### 风险

- F1 切换前盘点遗漏 → 线上 client 签发被拒：以最小默认集渐进放行，先
  告警后强制。
- D3 期间浏览器 token 仍暴露：M0-M2 之间 console 维持"加固中间态"
  （nginx 仅代理白名单路径、禁 admin 写）。
- F7 语义差异（本地验签看不到撤销）被遗忘 → 事件响应演练覆盖"凭据
  泄露→secret 轮换→下游 cache 清空"全链路。

### 可执行验收检查

1. `AllowedScopes` 空 client 请求任意 scope → 拒绝（单元测试断言
   scope.go 规则②改为 fail-closed）；
2. 枚举测试：admin 路由表 × 审计事件表全覆盖（F6）；
3. e2e：sverpweb 以 reader 账号查询 → 仅返回租户限定行；token 中无
   `cross_tenant`；
4. 集成测试：vault 部门移除成员 → sync 后 ACL 拒绝；sverpweb 撤销共享 →
   vault `access/check` 拒绝（F4/F5）；
5. aero-id e2e：配置 `jwks_url`、关闭 snaplink → 本地验签仍工作；
   撤销 token → cache stale 窗口内拒绝（文档化窗口）；
6. governance：draft schema 不参与摄入校验，approve 后生效（F8）；
7. nginx 集成：浏览器 token 到达上游前被剥离；BFF 服务账号 scope 枚举
   与路由注册表一致；governance 事件含 decision_id（F2）；
8. 轮换演练：轮换后旧 key 保持 verify-only ≥ 2×3600s，vault 刷新成功
   （F16）。

## 5. 待所有者/产品决策的未知项

1. **≤1s 校验延迟需求真实性**（D1-B 的唯一依据）——需求归属方确认，否则
   本地 JWKS 定案。
2. **审计 scope 命名**：保留现状 `audit:event:write`/`audit:legal_hold:manage`
   还是按 scope-matrix 提案改名（F10）——平台安全负责人裁决，启用连接
   前必须定。
3. **aero-im token 模型**：维持自签发 JWT 还是迁移 snaplink 签发（F12）——
   影响"im token 可否被其他服务接受"的信任边界；建议维持现状并成文。
4. **组织目录所有权**：确认 vault 为 organization/department 唯一权威
   （F11，platform-map 修正）——平台负责人签字。
5. **通知偏好权威源**：aero-im vs aero-id 二选一防双写（铃铛需求范围）——
   产品决策。
6. **legal hold 释放/下载双人控制**（决策 #6）是否经 BFF 落实——合规/
   安全确认。
7. **DM 定向通知触发条件**：subject→participant 映射表建设优先级——
   产品排期。
8. **BFF 部署归属**：nginx 内嵌改造还是独立服务——运维拓扑决策（影响
   D3 工期与故障域）。
9. **BFF 服务账号的租户上下文**：governance 读侧 tenant-scoped 语义对
   BFF 账号是否保持（跨租户时间线谁可见）——权限模型确认。
10. **部门同步触发方式**：周期 vs 事件驱动（F4 修复面）——依赖 vault/
    sverpweb 双方排期。
