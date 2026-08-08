Evidence grounding complete. I verified the on-disk artifacts (connection docs, `connections-evidence.md`, `scope-matrix-draft`, `integration-design-draft`, `audit-event-registry-draft`, `connections/inputs/`) against the supplied evidence. Below is the compliance review.

---

# Compliance Review — Snaplink Platform 连接子系统（7 项目）

**审查范围**：snaplink（IdP/OIDC+admin）、aero-id、aero-vault、aero-im、audit-governance、snaplink-console、sverpweb 的认证/授权/审计连接面。
**证据基线**：2026-08-08 连接分析流水线产物（7 份连接文档摘要，本审查输入）+ 我本次实际核查的仓库文件（`docs/snaplink-platform/connections/*.md` 及 `inputs/`、`scope-matrix-draft.md`、`integration-design-draft.md`、`audit-event-registry-draft.md`、`connections-evidence.md`）。
**独立验证说明**：输入中标注 Verified 的代码级结论（`/home/dwp/{snaplink,aero-id,aero-vault,aero-im,audit-governance,snaplink-console,sverpweb}`）**我未重新执行**，视为上游已验证证据；我独立验证的是：产物文件存在性/篇幅、inputs 留存、设计草案状态（均 Verified）。本报告是咨询性分析，不构成法律意见、合规声明或认证结论。

---

## 1. 适用范围声明（Applicable-scope Statement）

**适用对象**：本子系统作为**内部部署的 ERP 组合平台**（部署基线 192.168.123.52，self-hosted），其控制面（身份签发、服务账号授权、审计账本、文件访问、管理 BFF）是本审查的对象。出站依赖：无强制出站（snaplink 出站全为 opt-in 导出）——**无外部 SaaS 供应商链路可评估**。

**管辖区：UNKNOWN（未确立）**。输入未提供业务注册地、数据主体所在司法辖区、数据处理者/控制者角色、数据驻留要求。因此：
- **不宣称适用**：GDPR / PIPL / 等保 2.0（MLPS）/ ISO 27001 / SOC 2 / HIPAA / 中国个保法 等任何具体法规或框架——输入不能确立其适用性。
- **作为"assurance control family"映射而非认证声明**：下文控制矩阵按通用控制族（访问控制、审计、加密密钥、数据生命周期、事件响应、供应商）组织，仅描述"证据表明系统具备/缺失的工程控制"，**不意味着任何框架达标**。
- **内部契约控制（适用）**：scope-matrix（4 flows + 跨切 deny 规则）、integration-design（outbox 契约、ADRD-0002 schema 校验）、`MULTI_REPO_OPERATIONS.md` contract rules、`console-browser-bypasses-service-api` 禁止项——这些是工程契约，本审查将其作为**内部 contractual controls** 评估，其中被违反的禁止项按硬门禁违规处理。

**数据分类：UNKNOWN**。证据未确立任何数据的分类（PII/健康/财务/内部），亦无数据清单（data inventory）文档；aero-id 的"生命周期导出抹除"端点暗示存在个人数据，但 lawful purpose、minimization 依据均未成文。

**排除项**：legal-hold 作为**监管义务**不宣称（系统实现了 legal-hold 能力≠履行某法律义务）；vendor attestation（无外部供应商）；人员背景调查/培训（无证据，UNKNOWN）。

---

## 2. 控制矩阵（Control Matrix）

图例：✅ Verified（输入或我验证）｜🟡 Partial｜❌ Missing｜📝 Proposed（草案/建议）｜❓ Unknown

| # | 控制要求 | 状态 | 代码/仓库证据 | 流程证据 | 差距 | 责任人 | 验证方法 |
|---|---|---|---|---|---|---|---|
| C1 | **最小权限：client 级 scope 签发 allowlist** | 🟡 | snaplink `/token` `GrantedScopes` 共享门、per-client `AllowedScopes`（Verified） | scope-matrix 注册表为 📝 | **`AllowedScopes` 为空 = 完全不设限**（fail-open，规则②） | snaplink 组 | 建空 allowlist client 请求越界 scope，期望拒绝（当前会放行） |
| C2 | **服务账号读写分离** | ❌ | sverp-audit 现状 `audit:event:read audit:platform:cross_tenant` + 代码硬编码 tenantId（Verified 权限放大） | governance C-1、sverpweb B-3 为 📝 | 读账号未建、跨租户豁免在写/读共用凭据 | sverpweb + governance | 签发 `sverp-audit-reader` 仅读+租户限定，断言旧凭据读接口 403 |
| C3 | **admin 权限粒度** | ❌ | vault `requireAdmin`（注册表关闭时**隐式放行**，Verified）；`sverp-vault` 持整段 admin scope 仅用于部门同步；console 浏览器持 admin OIDC token 直连（命中 `console-browser-bypasses-service-api` 禁止项） | 两 token 模型 B-1/B-3、vault C-5 拆 scope 为 📝 | 粒度拆分未落地；隐式放行是生产隐患 | snaplink-console + vault | 浏览器 token 剥离测试（nginx 层）；vault 注册表开启后无 scope client 请求 401 |
| C4 | **审计：特权操作全覆盖** | ❌ | snaplink HTTP admin 疑似缺口：branding PUT/DELETE、backup、admin token revoke、dr/mode、access-policy converge、netpolicy（未见 Record 调用，Partial）；vault 部门管理 4 个写端点无审计（Verified）；console mutation 仅 localStorage 日志（可清除可伪造） | C-2/B-4 修复为 📝 | 无法对特权动作给出完整服务端审计轨迹 | 各服务组 | 枚举 admin 端点清单 × 审计事件存在性断言（gofmt 风格静态核对） |
| C5 | **审计：生产方覆盖** | 🟡 | sverpweb 338 事件全 published（Verified）；vault/id connector 已实现但 `enabled:false`（Verified）；aero-im **无 connector**（Verified gap）；snaplink 无 connector（Verified gap，L1 聚合选择） | integration-design §2 滚动序为 📝 | 4/6 生产方未实际入账；aero-id 直报（`wait_for=ledgered`）与 outbox 目标态语义不符 | governance + 各生产方 | 启用后按 schema 对账每服务事件数/幂等率 |
| C6 | **审计账本完整性** | ✅ | governance 哈希链/`integrity/verify`/append-only/batch 逐条幂等 receipt/双人 restore（Verified）；自审计 AdminAction 同事务 append fail-closed（Verified） | — | 无 | governance | `integrity/verify` 端到端 + 篡改注入测试 |
| C7 | **Schema 治理（摄入契约）** | ❌ | `audit:policy:write` 可单方注册、**立即生效无审批链**（Verified） | C-2 draft→approved 状态机为 📝 | 误注册直接放宽入账校验面 | governance | 注册未批准 schema → 摄入应 422（当前接受） |
| C8 | **密钥管理：签发与验签** | 🟡 | snaplink JWKS（single-flight+ETag，Verified）；governance 远程验签 kid 唯一/alg 严格/私钥材料禁止/HTTPS 强制（Verified）；**aero-id 默认 gRPC `sso.v1.SSO/ValidateToken` 指向 snaplink 不存在的服务**（Verified）；aero-im 自签发 JWT（issuer "aero-im"，RS256 自持密钥） | C-3 轮换 grace 契约（≥2×最慢下游刷新，vault 3600s）为 📝 | 轮换/compromise 流程未成文；aero-im 密钥保管/轮换 ❓；aero-id 默认配置不可用 | snaplink + aero-id + aero-im | 配置矩阵核对（默认值 → 实测调用）；轮换演练脚本 |
| C9 | **加密传输** | 🟡 | governance 强制 HTTPS（Verified）；内网部署 192.168.123.52，服务间链路加密 ❓（未见 mTLS 证据） | mTLS 已否决（📝） | 内网明文面未审计 | 平台 SRE | 抓包/配置核对内部端点 TLS 终止点 |
| C10 | **数据留存/删除** | 🟡 | governance retention policies 存在（Verified）；vault 删除同事务发 `file.deleted` fact（Verified）；registry 中 retention class 全为 📝 | — | **撤销/删除不级联 vault ACL**（残留授权）；部门成员只增不减 → 调岗/离职残留权限（Verified） | vault + sverpweb | 删除用户 → 断言 ACL 残留为 0 的回归测试 |
| C11 | **数据最小化** | 🟡 | 验签 claim 最小化提案（subject/tenant/expiry/scopes，无 profile PII，📝）；scope 命名 `<domain>:<resource>:<action>` 约定（Verified） | — | token 校验字段集未定案（C-4 三选一未决） | snaplink | 定案后按 claim 白名单断言 userinfo/validate 响应 |
| C12 | **事件响应** | ❌ | integration-design 10 故障模式 + DLQ 告警阈值（📝）；aero-id 人工重放 runbook（📝）；C-3 compromise 通知流程（📝） | **无 IR 计划/升级路径/事件分类**（❓/Missing） | 全部为设计草案，无演练、无值班契约 | 平台 SRE | 故障注入演练（DLQ 毒丸、sink 宕机、密钥泄露剧本） |
| C13 | **双人控制** | 🟡 | restore approve/reject 双人（Verified）；decision #6 释放/下载双人（📝） | — | 高风险操作清单未固化 | governance + console | 双人流程端到端测试 |
| C14 | **租户隔离** | 🟡 | governance 非写权限强制 tenant 上下文（Verified）；vault tenant claim 映射（Verified）；aero-id `RequireTenant`（Verified） | — | `audit:platform:cross_tenant` 豁免在宽泛凭据上（C2）；aero-id 启用前须 tenant bindings 覆盖校验（📝） | 各服务组 | 跨租户读取断言（无豁免账号应 403） |
| C15 | **数据清单/合法目的** | ❌ | aero-id 自服务导出/抹除端点存在（Verified，暗示个人数据） | **无 data inventory、无 privacy notice、无 lawful basis、无 DPIA** | 全部 Missing | 产品/法务 | 人工：数据流清单 + 法律依据文件（非仓库可验证） |
| C16 | **scope 命名一致性** | 🟡 | 现状 `audit:event:write` ≠ 提案 `audit:event:ingest`（Verified 名）；governance 权限名与凭据 scope 恰好一致（Verified） | 注册表为 📝 | 命名漂移会破坏 fail-closed 校验与审计归因 | governance + snaplink | 注册表落地后全量凭据×权限交叉断言 |

---

## 3. Findings（按严重级排序）

### Critical

**F1 — scope 注册表缺失：空 allowlist client 完全放行（fail-open 授权）**
- 证据：snaplink `GrantedScopes` 规则②——`AllowedScopes` 为空时 client 不设限（输入 Verified；C-1）。
- 风险：任一 client 配置疏漏 = 无限制签发，可横向触及全平台管理面；这是授权体系的可利用缺口，且阻断 C-4 与连接 8 的前置链。
- 修复：注册表落地（P0）+ **默认拒绝**（空 allowlist = 零 scope），越界 scope 整体拒绝语义保留；注册表变更走审批。
- 关闭证据：fail-closed 单元测试（空 allowlist → 所有 scope 拒绝）+ 注册表 schema 审查记录。

**F2 — 浏览器 admin token 直连（硬门禁违规，`console-browser-bypasses-service-api`）**
- 证据：console 浏览器持 admin 用户 OIDC token 原样透传 snaplink（连接 5，Verified）；`admin:*` 通配符存在（Verified）；governance/vault/id/im 零 upstream。
- 风险：token 失窃 = 全平台管理权限；且治理账本当前**完全不可达**（无路由）——管理侧对治理数据的可见性为零。违规项已被架构禁止但仍在线。
- 修复：两 token 模型落地（B-1：BFF 服务账号持枚举 admin scope，禁通配符）+ nginx 强制剥离浏览器 token + `decision_id` 契约桥归因；governance 白名单路由（B-2）。
- 关闭证据：nginx 剥离规则测试（浏览器 token 到达上游即 401）+ BFF 服务账号 scope 枚举清单 + 端点矩阵。

### High

**F3 — aero-id 默认 gRPC 校验指向不存在的服务**
- 证据：默认 `sso.v1.SSO/ValidateToken`（proto 在 aero-id 仓库）但 snaplink 侧无该服务（Verified）；`jwks_url` 配置即切本地验证，二选一。
- 风险：默认配置下认证路径不可用（可用性）或配置漂移导致验签语义与预期不符（撤回语义差异是决策核心，C-1）。
- 修复：定案三选一（推荐本地 JWKS：零服务端改动）；若选 gRPC，须定契约归属 + snaplink 新增服务 + 配置默认值显式化。
- 关闭证据：决策记录（ADR）+ 配置矩阵测试（默认值 → 实测认证流）。

**F4 — 特权操作审计覆盖缺口**
- 证据：snaplink HTTP admin 疑似缺口 6 类（branding/backup/token revoke/dr-mode/access-policy/netpolicy，Partial）；vault 部门管理写端点无审计（Verified）；console mutation 仅 localStorage（可清除可伪造，Verified）。
- 风险：特权动作无法从服务端账本还原——审计完整性要求的直接违反；console 本地日志可伪造，不能作为证据。
- 修复：snaplink C-2（HTTP 面补 Record 调用 + gRPC interceptor 全覆盖校验）；vault C-3（部门写端点同事务审计）；console B-4（服务端审计为权威、本地日志降级 UX 辅助）。
- 关闭证据：admin 端点×事件类型存在性静态断言 + 抽样 e2e（console 操作 → 账本出现对应事件）。

**F5 — 服务账号权限放大**
- 证据：sverp-audit 持 `audit:event:read + audit:platform:cross_tenant` 且硬编码 tenantId（Verified）；sverp-vault 持整段 admin scope 仅为部门同步（Verified）；scope 名 `audit:event:write` vs `audit:event:ingest` 漂移（Verified）。
- 风险：读/写共用凭据 + 跨租户豁免 = 单凭据失窃即可跨租户读取；宽泛 admin scope 扩大爆破面。
- 修复：audit-governance C-1 + sverpweb B-3（`sverp-audit-reader` 租户限定只读）；vault C-5（拆 `vault:dept:manage` 或迁 console BFF）；命名统一（C-1 注册表同步）。
- 关闭证据：新凭据矩阵 + 旧凭据对读接口 403 回归。

**F6 — Schema 单方注册、立即生效**
- 证据：`audit:policy:write` 持有者可直接注册 schema，摄入 fail-closed 直接引用 Active schema（Verified）。
- 风险：误/错注册直接放宽入账校验面，且无追溯——账本信任基础的契约失控。
- 修复：C-2 draft→approved 状态机 + 第二人批准（复用 restores 双人语义）；摄入只接受 approved。
- 关闭证据：未批准 schema 摄入 422 测试 + 审批流 e2e。

**F7 — 残留授权（删除/撤销不级联）**
- 证据：撤销/删除不级联 vault ACL（sverpweb B-4①，Verified）；部门同步仅启动时一次 + 成员只增不减 → 调岗/离职残留权限（vault C-2，Verified）。
- 风险：离职/调岗后文件与部门资源持续可访问——访问撤销义务的直接违反。
- 修复：`syncDepartment` 补差集 DELETE；删除路径级联策略成文；四路径授权语义表 + 回归测试（sverpweb B-4）。
- 关闭证据：删除用户后 ACL 残留为零的自动化断言。

**F8 — 审计生产方未全部入账**
- 证据：aero-im 无 connector（Verified gap）；snaplink 无 connector（Verified gap）；vault/id 实现但默认关闭（Verified）；aero-id 直报语义与 outbox 目标态不符（Verified）。
- 风险：组合平台对 4/6 生产方的操作无中央可见性——事件响应与合规取证盲区。
- 修复：integration-design §2 滚动序（sink→id→vault→snaplink→im→console）；aero-im C-1 outbox relay（scope 授予以其为前提）；启用前完成 tenant bindings 覆盖校验（aero-id C-2）。
- 关闭证据：启用后每服务事件对账 + 幂等率 + DLQ 告警演练。

**F9 — 密钥轮换与 compromise 流程未成文**
- 证据：JWKS 轮换 grace 契约（≥2×3600s）为 📝；compromise 无 overlap 路径为 📝；aero-im 自持密钥保管/轮换 ❓。
- 风险：密钥材料生命周期不可审计；无应急剧本时轮换期服务中断或泄露期残留信任。
- 修复：C-3 契约落地（grace 双倍最慢下游刷新 + 通知矩阵）+ aero-im 密钥保管入 vault + 轮换演练。
- 关闭证据：轮换演练记录（含下游 vault 刷新窗口验证）+ aero-im 密钥来源审计。

### Medium

**F10 — 留存策略与 registry 全为草案**
- 证据：audit-event-registry-draft 明确 DRAFT；retention class、legal-hold sensitivity 全 [PROPOSED]。
- 风险：留存/销毁时限未定案，账本无限增长或销毁过早的合规风险未裁决。
- 修复：registry 走人工批准提升为契约（`MULTI_REPO_OPERATIONS.md` 流程）+ retention 分级定案。
- 关闭证据：批准记录 + 分级 retention 策略配置生效测试。

**F11 — 分析产物证据留存不一致**
- 证据（我独立验证）：输入声称各连接文档 241/200/208/169/179/139/143 行且位于 `docs/snaplink-platform/connections/*.md`，但磁盘上这些路径实为 11–21 行摘要；完整版文档未在所述路径找到（inputs/ 目录留存的是流水线输入而非全文输出）。
- 风险：审计-就绪性要求"评审结论可追溯到完整证据"；若仅摘要留存，后续审计无法复核分析依据。
- 修复：全文产物归档（`docs/archive/` 或 connections/full/），摘要保留指针。
- 关闭证据：全文文档落盘路径 + 行数核对。

**F12 — 事件响应与运营程序缺文档**
- 证据：IR 计划/升级路径/值班契约 Missing；DLQ 告警阈值、人工重放 runbook 均 📝。
- 风险：账本中断、密钥泄露等场景无既定程序，响应时效不可控。
- 修复：IR 计划 + 3 个 runbook（重放/DLQ/轮换）+ 故障注入演练。
- 关闭证据：演练记录（时间、参与者、结论）。

**F13 — 内网链路加密未审计**
- 证据：HTTPS 强制仅见 governance JWKS 面；内网服务间传输 ❓。
- 修复：平台 SRE 核查内网链路 TLS 终止点/加密。
- 关闭证据：网络配置核对记录。

### Low / Info

**F14 — 数据清单与合法目的缺失（F15 域）**：aero-id 导出/抹除端点暗示个人数据，但 data inventory、lawful basis、DPIA、分类均 Missing（❓）。修复：人工补齐数据流清单与法律依据——非仓库可验证项，列为合规前置待办。
**F15 — 双人控制覆盖不全**：仅 restore 实现（Verified）；decision #6 释放/下载双人、legal-hold 释放为 📝。低风险项，随 B-2 落地。
**F16 — 信息类**：localStorage 日志仅 UX 辅助（B-4 已明确不提升为证据源）——设计正确，需保持。

---

## 4. 审计就绪总结（Audit-readiness Summary）

**结论：当前不具备任何外部审计/认证就绪状态**——技术控制有扎实基础（账本完整性 C6、租户隔离 C14 部分、验签严格性 C8 部分），但 2 个 Critical（F1/F2）、7 个 High（F3–F9）阻断就绪声明。所有控制均须人工核准后才可对外表述（"Implemented controls are not evidence of certification"）。

**审计前必须产出的文档**（当前状态：Missing/📝）：
1. **Scope 注册表**（F1，P0，fail-closed 默认）——同时是 C-4 与连接 8 的前置链根节点
2. **审计事件注册表**：draft → 批准契约（F10，含 retention 分级）
3. **两 token BFF 设计 + nginx 剥离规则**（F2，含四元组路由注册表 B-3）
4. **密钥轮换与 compromise runbook**（F9）
5. **IR 计划 + DLQ/重放 runbook**（F12）
6. **数据清单、分类与合法目的文件**（F14，jurisdiction 未确立时先行内部版本）
7. **连接分析全文产物归档**（F11，证据可追溯性）

**下一步决策（按依赖序）**：
1. C-1 注册表落地 + 空 allowlist 改默认拒绝（阻塞项）
2. gRPC 校验定案：本地 JWKS vs 新增 validate 服务（aero-id C-1 / snaplink C-4）
3. 审计启用排序：vault/id 启用 + aero-im connector（scope 授予条件）+ snaplink 上报取舍
4. Schema 审批链状态机
5. 读/写账号分离 + `audit:event:ingest` 命名统一
6. 双人控制清单固化（restores 为锚点扩展）
7. 残留授权修复（vault 级联删除 + 部门差集同步）排入实现批次

**合规立场声明**：本报告不构成法律建议、不宣称任何法规/框架达标；管辖区、数据分类与框架适用性均 UNKNOWN，须由法务/合规职能确认后重新映射。所有"Verified"均为仓库代码级证据，外部审计认可仍需独立验证（SOC 2 型、等保测评等外部程序）。

---

**审查结论**：控制基础（append-only 账本、验签严格性、幂等接收、租户强制）优于一般内部平台；当前主要风险集中在**授权默认值与权限放大**（F1/F2/F5）、**特权操作审计完整性**（F4）与**残余授权**（F7）三个方向，与流水线产出的 C-1→C-4→连接 8 前置链一致。建议将 F1/F2 作为平台发布阻塞项，其余按依赖序排入 implementation batches。
