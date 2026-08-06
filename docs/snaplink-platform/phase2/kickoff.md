预验证全绿：当前第四次运行配置（自定义标记）PASS、默认标记集 PASS、头部无自指、200 行 ≥ 60。以下为交付文档正文（与已验证的 `/tmp/kickoff-final.md` 一致）：

# 第二阶段评估：平台现状与剩余路线图（2026-08-06）

> 评估基线：证据 05（2026-08-06 部署/实现后实测，192.168.123.52 / svmisdev / Debian 12），
> 交叉核对证据 01/02/04、`DEPLOYMENT_192.168.123.52.md`、`TESTING_GUIDE_192.168.123.52.md`、
> `ERP_SCENARIO_ANALYSIS.md`。每条结论标注 **Verified**（源码/实测/文档实证）或 **Proposed**
> （推理建议）。本文件是提案，经人工核对后提升；不豁免任何仓库门禁。

## 0. 评估基线（Verified）

- **部署现状（8 服务 systemctl 全 active，Verified）**：snaplink-sverp :13015/:13016（sqlite+JWT，**重启即轮换签名密钥**）、snaplink-console :13017（nginx Flutter Web）、sverpweb-stable :13014（Hono BFF + Vue3/iris-ui 0.2.x）、audit-governance :13018（文件账本单节点 state.json，`AUDIT_ALLOW_DEV_AUTH=false`，Ed25519 PEM 公钥注入）、aero-vault :13019（sqlite+local，Snaplink JWKS 认证）、aero-id :13020/:13021（PG+local+snaplink gRPC）、aero-im :13022（PG+Redis+NATS JetStream）、nats :13023。基础设施 PG15/Redis/pgvector/NATS 齐备；公钥同步 timer（sync-sso-key，2min 轮询）运行中。
- **第一轮 gate 阻塞项 B1–B5 全部闭环（Verified）**：tsc 31→0、forwarding 8 用例、permanent 列+毒丸隔离+逐事件 receipts、erp_contract_test.go 5 测试、git init（commit d546647）、审计 L2 E2E 全通（质检单 → outbox → governance-transport 适配器 → 账本 actor=admin 无重复）。
- **里程碑达成状态（Verified）**：M0 契约对拍 ✅；M1 准入门禁（tsc 0 错 + 测试全绿 + forwarding 8 用例）✅；M2 验收（审计 L2 E2E 全通 + test-platform.sh status 25/audit 8/e2e 8 全绿 + 集成测试页三服务通过 + presign 直传 + IM 质检通知）✅。
- **账号资产（Verified）**：admin（SSO 管理+ERP 全权限）、tester（质检最小 5 项权限，403 边界实测）；服务账号 sso-admin（admin:* + audit 读/写/policy + platform）、sverp-audit（audit:* + platform）、sverp-vault（read/write + client→tenant 映射）；client sverp-web（OIDC 授权码+PKCE）。
- **关键判定（Verified 现状 + Proposed 推论）**：M2 的 **E2E 已全通，但"宣布 L2"的前置未全绿**——原路线图 L2 前置含"demo 禁用"（DEMO_PASSWORD 仍在，05 §4）、"真实密钥轮换"（当前是重启即轮换，语义未受控）、HTTPS（未做，JWKS 因无 HTTPS 才退化为 PEM timer 同步）。因此剩余路线图的**第一优先级不是新功能，而是生产硬化收尾**。
- 已被实现推翻的旧论断不得再引用（"设 env 即激活"作废、B2 actor 丢失已修、B4 零审计事实已反转）。

# 修订版最终路线图（第二阶段重排）

## 1.1 重排原则（Proposed）

1. **门禁前置优先**：L2 宣布前置（生产硬化）> 用户可见面（console 审计时间线）> 数据风险收敛（vault 迁移，须备份体系就位后启动）> 低成本运营杠杆（M4 pi-batch 嵌入）> 增值功能（M5 通知扩展）> 无需求信号项（aero-id 组织，维持 H2）。
2. 与第一轮排序的差异（Proposed）：vault 迁移（原 M3 前列）**后移**至备份体系（4.3）就位之后——迁移涉及数据风险与回填校验，且 presign 直传已验证可独立先用；console 审计时间线（原与 vault 并列 M3）**前移**——它是 L2 收尾的可见面、零数据风险；M4 pi-batch 嵌入**提前**——git init 前置已达成（B5 修复），成本极低且解锁第 7 仓 7×24 轮巡（4.1 依赖）。

## 1.2 里程碑表（重排后定序，验收标准均为可执行断言）

| 序 | 里程碑 | 价值 | 成本 | 依赖/前置 | 验收标准（摘要，详见各小节） |
|---|---|---|---|---|---|
| 1.2.1 | **生产硬化-1：密钥轮换语义修复**（snaplink 单仓库） | 高：消除"每次重启全链路认证失效窗口" | 低（半天级） | 无 | 重启演练全绿且无需手工同步公钥 |
| 1.2.2 | **生产硬化-2：L2 宣布前置收尾**（DEMO_PASSWORD 清除 + 配置 fail-closed + HTTPS 豁免决策） | 高：L2 正式宣布的唯一阻塞 | 低 | 1.2.1 | L2 checklist 全勾（含豁免理由） |
| 1.2.3 | **M3a：console 审计时间线接 governance**（console 仓 Flutter 侧） | 高：统一审计时间线 = L2 用户可见承诺 | 中 | 1.2.2 读侧授权裁定 | 质检单事件进 console 时间线 |
| 1.2.4 | **M4：sverpweb 嵌入 pi-batch**（薄壳+pi-batch.yaml+CI 门禁） | 中：解锁第 7 仓 7×24 轮巡与 campaign 分析面 | 极低 | 无（git init 已达成） | platform-247 第 7 仓不 SKIP |
| 1.2.5 | **M3b：vault 生产数据迁移**（懒迁移+回填+双读禁双写） | 高：数据风险收敛（媒体脱离无备份的服务器本地目录） | 中高 | 备份体系（4.3）就位 | 回填校验一致 + 切流无丢失 |
| 1.2.6 | **M5：通知扩展**（审批/异常/任务分派） | 中 | 低 | 业务事件面确认 | ≥2 新触发点 + 故障注入不阻塞 |
| 1.2.7 | **aero-id 组织投影**（维持 H2 缓接） | 低（无业务需求信号） | 中 | 业务数据成熟 | 不排期，仅记录触发条件 |

### 1.2.1 生产硬化-1：密钥轮换语义修复（P0，Verified 现状 + Proposed 处置）

- **证据（Verified）**：snaplink-sverp 每次重启轮换签名密钥，`rotation.enabled=false` 仍轮换（05 §4）；影响链实测——governance 用 PEM 公钥注入（DEPLOYMENT §4：jwx 不支持 JWK JSON，JWKS 需 HTTPS）、vault 用 SDK JWKSCache、sverpweb 客户端 401 自动刷新重试、公钥靠 sync-sso-key timer 每 2min 同步；TESTING_GUIDE FAQ 实证"audit 投递 401 → 重启轮换密钥"为已知故障模式。
- **处置（Proposed）**：修复 snaplink 轮换语义——`rotation.enabled=false` 必须真正禁用轮换（密钥持久化，仅显式 `rotate` 命令才轮换）；轮换改为显式生命周期操作并输出新旧 kid 告警。**短期保留 timer 同步作为兜底**（2min 窗口 + 客户端重试实测可接受），不急于上 HTTPS+JWKS（决策见 2.1）。
- **验收标准**：`systemctl restart snaplink-sverp` 后 `test-platform.sh status/audit/e2e` 全绿**且无需手工跑 sync-sso-key.sh**（重启不再产生新密钥）；或（采用显式轮换）`rotate` 后 2min 内三套件自动收敛；新增 snaplink 单测：rotation.enabled=false 时跨重启密钥指纹不变。

### 1.2.2 生产硬化-2：L2 宣布前置收尾（P0）

- **证据（Verified）**：L2 前置清单（ERP_SCENARIO_ANALYSIS §2）中 `AUDIT_ALLOW_DEV_AUTH=false` ✅、读侧授权（账本查询需 Bearer + policy scope，TESTING_GUIDE §4 实证 401 门禁）✅；**未完成项**：demo 禁用（05 §4：DEMO_PASSWORD 死配置仍在）、密钥轮换受控（1.2.1）、HTTPS（未做，需显式决策：本机内网可豁免或自签）。
- **处置（Proposed）**：清除 DEMO_PASSWORD（2.2）；sverpweb 配置校验 fail-closed（未知/demo 键启动即拒）；HTTPS 决策显式化——**当前单机内网豁免，写入部署文档与触发条件**（外部暴露/多主机/合规要求时启用，见 2.1）；重启演练固化为 test-platform.sh 子命令（4.4）。
- **验收标准**：L2 宣布 checklist 文档化并全部勾选（豁免项带理由）；重启演练脚本通过；`grep -r DEMO_` 生产 .env 零命中。

### 1.2.3 M3a：console 审计时间线接 governance（P1）

- **证据（Verified）**：snaplink-console :13017 审计页目前为本地管理操作记录（`audit_log_service.dart` + LocalStorage，证据 02/TESTING_GUIDE §8 一致）；governance 查询 API 已实证可用（TESTING_GUIDE §4：`/api/v1/events?tenant_id=sverp&source_system=sverp-web` + Bearer）；console 原则"以后端契约为准、不拥有业务数据"（证据 02）。
- **改动面（Proposed）**：console 仓库 audit 服务页改调 governance 读 API（source 过滤/分页/actor 展示），sverp-audit 授权（联动 3.2 最小权限）；**跨仓库契约变更须走 suite 契约测试/Change Manifest**——默认只改 console + 确认 governance 读 API 契约，governance 大概率零改动。
- **验收标准**：创建质检单 → drainer 窗口内事件出现在 console 审计时间线（source=sverp-web、actor=admin）；console 无直连 DB、无本地业务数据副本；`test-platform.sh` 新增 console 审计页检查项。

### 1.2.4 M4：sverpweb 嵌入 pi-batch（P1）

- **证据（Verified）**：git init + 基线提交 d546647 已达成（B5 修复）；`pi-batch-campaign.yaml` 已就位（20 模块发现，dry-run 零成本，ERP_SCENARIO_ANALYSIS §3.1）；platform-247.sh 已将 sverpweb-stable 列为第 7 仓但无 pi-batch.py 时 SKIP（脚本实证）。
- **改动面（Proposed）**：复制薄壳 pi-batch.py + pbatch/ + pi-batch.yaml（含 pyquality/completion 验证器）；campaign 仅分析面（`git_commit: false` 保持）；CI 门禁（`pnpm check` 入 git 钩子或 CI）。
- **验收标准**：`platform-247.sh 1 120 sverpweb-stable` 不再 SKIP 且跑完一轮滚动分析；`pi-batch.py campaign --dry-run` 在本仓零报错；`pi-batch check` 通过。

### 1.2.5 M3b：vault 生产数据迁移（P1，依赖 4.3 备份就绪）

- **证据（Verified）**：ERP 媒体仍本地 `apps/api/src/media/sqlite-store.ts`（证据 01）；presign 直传/下载/删除已实测通过（TESTING_GUIDE §8，BFF 只签发 URL 不缓冲文件流）；sverp-vault 服务账号 read/write + client→tenant 映射就位（05 §3）；vault nil-panic 已修（commit c0ce6db）。
- **改动面（Proposed）**：新建上传/下载走 vault presign；读路径**双读**（vault 优先、SQLite 回退）——**禁双写**（采纳第一轮裁决"presign 直传禁双写"）；懒迁移 + 回填任务（计数/校验和核对）；切流开关 + 全量校验通过后关闭 SQLite 读路径。
- **验收标准**：存量媒体回填后数量与字节数一致（校验脚本）；新上传零落盘服务器本地；关闭回退后旧链接仍可访问（迁移期 URL 兼容）；`test-platform.sh` 新增 vault 生命周期子命令覆盖上传/下载/删除/越权 403。

### 1.2.6 M5：通知扩展（P2）

- **证据（Verified）**：当前仅质检创建 1 个触发点（05 §4）；aero-im :13022 "SVERP Integration" 频道实测 NOTIFY-TEST-001/002 8s 内送达、best_effort 不阻塞业务且审计仍入账（TESTING_GUIDE §8）；原路线图目标事件面：审批/异常/任务分派 3 类。
- **处置（Proposed）**：先确认 ERP 业务事件面（APS 排程变更/精准报价审批是否有真实状态机——现三条路由为 quality/APS/bs-standard-pricing）；仅对真实存在的状态变化加触发点；模板/偏好放 aero-im（notification-preference 现成）；failure_policy 与审计对齐（异步不阻塞）。
- **验收标准**：≥2 个新触发点带单测；故障注入（aero-im 停机）下业务主流程与审计链路不受影响；通知送达计入 test-platform.sh e2e 断言。

### 1.2.7 aero-id 组织投影（维持 H2，不排期）

- **证据（Verified）**：H2 降级为第一轮明确决策（修订要求 5 条采纳项）；ERP 无组织树/人员选择器需求信号；DepartmentSnapshot 可空占位已做对（契约预留成立）。**触发条件（Proposed）**：出现组织维度业务需求（多部门质检权限/组织视图报表）时，先契约定义 + 投影，再 UI。
- **克制红线保持**：不得每请求调 aero-id（组合纪律）；gRPC 双传输能力在但不作为接入理由。

## 2. 新发现问题处置（证据 05 §4 逐条）

### 2.1 密钥轮换：HTTPS+JWKS 动态 vs timer 同步（决策矩阵）

- **现状（Verified）**：snaplink 重启即轮换；governance 因 jwx 不支持 JWK JSON 且 JWKS 需 HTTPS，退化为 PEM 静态注入 + timer 2min 同步；vault 走 JWKSCache；sverpweb 客户端 401 自动重试兜底。
- **决策（Proposed）**：
  1. **先修轮换语义（1.2.1）**——重启不应轮换；这是根因，不是同步问题。
  2. **短期维持 timer 同步**——实测 2min 窗口 + 客户端自动重试已把影响控制在可接受范围；成本为零。
  3. **HTTPS+JWKS 动态作为触发门控项**——触发条件：服务暴露到内网以外 / 主机数 >1 / 合规审计要求；届时 nginx 终结 TLS + snaplink 提供 `/.well-known/jwks.json` + governance 改用 JWKS 拉取（HTTPS 场景可绕过 jwx 的 JWK JSON 限制）。
- **验收标准**：1.2.1 落地后，重启演练全绿且无手工同步；JWKS 触发条件文档化。

### 2.2 DEMO_PASSWORD 死配置（清除 + fail-closed）

- **现状（Verified）**：sverpweb .env demo 模式仍有 DEMO_PASSWORD（05 §4，N2 待办）。
- **处置（Proposed）**：从 .env 与配置读取逻辑中删除；env 校验对未知/demo 键 fail-closed（启动即拒并给出具体键名）；全仓 grep DEMO_ 零命中（docs 除外）；新增配置校验单测。
- **验收标准**：`grep -n DEMO` 生产 .env 无输出；配置校验测试纳入 `pnpm check`。

### 2.3 静态 token 残留（AUDIT_APP_TOKEN 移除）

- **现状（Verified）**：sverpweb .env 仍有 AUDIT_APP_TOKEN；OAuth client_credentials 自动轮换（exp 缓存 + 提前刷新 + 401 强制重试）已实现且为优先路径（05 §2、DEPLOYMENT §6）。
- **处置（Proposed）**：删除 AUDIT_APP_TOKEN；transport 侧 OAuth 未配置时 **fail-closed**（禁止静默回退静态 token）；保留 401→重试路径测试。注意：sverp-audit/sso-admin **secret 本身**仍属引导凭据，归 4.2 密钥管理（轮换 + 迁移 keys.txt）。
- **验收标准**：`grep -rn AUDIT_APP_TOKEN apps/` 零命中（docs 除外）；e2e 审计链路在移除后全绿。

### 2.4 console 审计页缺口（归 1.2.3）

- 处置见 1.2.3。补充治理边界：console 只做 governance 的**读侧视图**，不建本地事件副本、不直连 DB；查询走 sverp-audit 授权账号（3.2 最小权限联动）。

### 2.5 snaplink-console inactive 事件复盘

- **现状（Verified）**：console 曾 inactive 一次（05 §4），根因未记录。
- **处置（Proposed）**：① 查 journalctl 定位根因（nginx 单元崩溃/重启）；② systemd 单元加 `Restart=on-failure`；③ `test-platform.sh status` 增加 console HTTP 200 健康检查（脚本现 25 项未显式含 console 探测，属 3.3 缺口）；④ 复盘结论沉淀为 learn 规则（若为资源/配置类，写入注册表）。
- **验收标准**：status 子命令含 console 探测；`systemctl kill -s KILL snaplink-console` 后 5s 内自动恢复 active。

## 3. 组合纪律核查

### 3.1 审计链路的真源裁定（仍成立，Verified + 1 项风险提示）

- **复核（Verified）**：E2E 实证"质检单 → outbox → 适配器 → 账本 actor=admin 无重复"，governance 是审计事实源、ERP 保留领域状态、审计异步不阻塞业务、L2 是部署规则——原裁定**仍成立**。permanent 毒丸隔离 + 逐事件 receipts + idempotency_key=event.id 幂等语义全部落地（04 修订要求 5 项采纳全部兑现）。
- **风险提示（Proposed）**：governance 是**单节点文件账本（state.json）**——"真源"的持久性依赖该文件备份（4.3 将其列为最高优先级）；多节点/复制需求出现前不引入（克制）。snaplink AuditSink（SSO 内部策略/RBAC/token 撤销进统一账本）仍为**未验证缺口**（证据 02 保留）——处置：**暂缓**，SSO 内部事件维持 L0/L1，触发条件=合规要求或出现 SSO 侧审计事故。
- **不接项保持**：替换 outbox（仅当 governance 提供事务性中继时重评）、OTel 替代审计——仍成立。

### 3.2 服务账号最小权限核查（Verified 现状 + Proposed 收窄）

| 账号 | 现状 scope（Verified 05 §3） | 最小权限裁定（Proposed） | 处置 |
|---|---|---|---|
| sso-admin | admin:* + audit 读/写/policy + platform | 过宽：审计查询不应走 sso-admin（TESTING_GUIDE §4 示例即用它查账本） | 查询改用 sverp-audit；sso-admin 仅保留 admin:* + 审计治理（policy/平台注册）；测试脚本同步改 |
| sverp-audit | audit:* + platform | 运行时发布方**不需要 policy:write**（schema/source 注册是治理操作，归 sso-admin）；运行时最小集 = audit:event:write + audit:event:read + audit:platform:cross_tenant | 收窄 scope；回归 e2e 证明收窄后链路仍绿；新增负向测试：sverp-audit 调 policy 写 → 403 |
| sverp-vault | read/write + client→tenant 映射 | 与 presign 直传 + BFF 列表/下载职责匹配，**基本合理** | 补越权测试：跨 tenant 读/写 → 403（当前无实证） |
| tester | 质检最小 5 项，403 边界实测（Verified） | 合理 | 保持；密码轮换（4.2） |

- **补充核查（Proposed）**：新增"scope 审计"动作——列出各账号有效权限并对照职责矩阵，纳入 test-platform.sh audit 子命令。

### 3.3 测试体系覆盖缺口（test-platform.sh，Verified 现状 + Proposed 缺口清单）

- **现状（Verified）**：status 25 / audit 8 / e2e 8 全绿；e2e = 登录 → 质检单 → outbox → 账本（TESTING_GUIDE §3）。
- **缺口（Proposed）**：
  1. **console 健康检查缺失**（2.5 直接关联）；
  2. **vault presign 生命周期不在脚本内**（集成页人工覆盖，脚本 e2e 不含上传/下载/删除/越权）；
  3. **IM 通知不在脚本断言**（NOTIFY-TEST-001/002 为人工实测）；
  4. **无负向用例**：sverp-audit 越权 policy 写、sverp-vault 跨 tenant、已撤销 token、过期 refresh；
  5. **无密钥轮换演练**（1.2.1 验收依赖）；
  6. **无备份/恢复演练**（4.3 依赖）；
  7. **无 drainer 稳态检查**（pending=0、permanent 计数=0 的 24h 观察——L2 宣布指标）；
  8. 脚本位于服务器 `/home/ywb/test-platform.sh` 未版本化——建议入 suite 仓库作为 ops 资产（版本化 + Change Manifest 门禁）。
- **处置（Proposed）**：新增子命令 `vault`/`im`/`rotation`/`negative`/`backup` 或扩展现有套件；缺口 1/5/6 与对应里程碑验收绑定。

## 4. 运营建议

### 4.1 7×24 巡检扩展（platform-247.sh）

- **现状（Verified）**：脚本已列第 7 仓（sverpweb-stable）但无 pi-batch.py 时 SKIP；当前只做滚动分析 + 重试，**无运行时健康阶段**。
- **扩展（Proposed）**：
  1. 1.2.4 落地后第 7 仓自动激活；
  2. 每轮追加运行时健康阶段：调 `test-platform.sh status`（含新增 console 检查 + drainer 指标），失败写入 summary 并在下一轮自动重试；
  3. 平台 status 看板加 governance 账本行（事件总数/最近事件时间/来源分布），一眼识别"审计停了"；
  4. 告警通道 dogfood：aero-im "SVERP Integration" 频道作为巡检告警 sink（成本低、现成渠道；克制：仅告警，不做复杂编排）。
- **验收标准**：连续 3 轮巡检无 SKIP；人为停掉 governance 后 2 轮内 summary 出现告警记录。

### 4.2 密钥管理

- **现状（Verified）**：keys.txt（0600，3 行：sso-admin secret/admin 密码/sverp-audit secret）与 accounts.txt（admin/tester 密码）在服务器明文可读路径；governance .env 0600；aero-im RSA-2048 keypair、aero-id signing_key、PG 口令（aero_local_dev_only，dev 级）各自散落。
- **建议（Proposed）**：
  1. **立即**：admin/tester 密码 + 三个服务账号 secret 全量轮换（部署实录已注明"应轮换并移入密钥管理"），更新 keys.txt；
  2. 建立**密钥清单表**（每项：位置/用途/轮换周期/owner）：snaplink 签名密钥（1.2.1 后显式轮换）、sso 服务账号 secret（30 天）、governance AUDIT_SIGNING_SECRET/AUDIT_ENCRYPTION_KEY（**不得随意轮换**——哈希链/加密历史依赖，备份优先）、aero-im RSA、aero-id signing_key、PG 口令；
  3. 服务器上 secret 迁入 systemd EnvironmentFile（0600，逐服务隔离），keys.txt 仅保留测试专用副本；集中式 secrets 管理（Vault/KMS）列为触发项（主机数>1 或合规时）；
  4. 提交钩子/扫描防 secret 入库（sverpweb 已 git init，立即生效）。

### 4.3 备份（当前零脚本，Proposed）

- **范围与优先级**：① governance state.json（审计真源，**最高**）；② snaplink sqlite（sso db + 密钥）；③ sverpweb sqlite（app.sqlite + 会话 + 本地媒体，**迁移完成前双备份**）；④ vault sqlite + local 文件；⑤ aero_id / aero_im PG（pg_dump）；console 静态产物可重建。
- **机制（Proposed）**：PG 每日 pg_dump（保留 7/30/90）；sqlite 用 `.backup`（一致性）；数据目录 rsync；**月度恢复演练**（写入 test-platform.sh backup 子命令）；备份目标与服务器分离（本地第二盘或内网 rsync 目标）。
- **验收标准**：演练脚本从备份恢复 governance 账本后，查询与 E2E 全绿；演练纳入 7×24 周节奏。

### 4.4 升级流程与重启停机窗口（Verified 现状 + Proposed runbook）

- **影响链（Verified）**：snaplink 重启 → 签名密钥轮换 → 存量 JWT/refresh token 失效（用户需重新登录）→ governance/vault 验证 PEM/JWKS 过期 → 审计投递 401（sverpweb 客户端自动重试 + 幂等安全网兜底）→ 2min timer 同步恢复。**当前重启 ≈ 全链路认证短暂中断 + 审计 401 窗口**。
- **建议（Proposed）**：
  1. 1.2.1 修复后重启不再产生此窗口（根本解决）；
  2. 维护窗口 runbook：**低峰期**执行；顺序 = 备份（4.3）→ 基础设施（PG/Redis/NATS）→ snaplink → governance/vault/aero-id/aero-im → console → **sverpweb 最后**（BFF 可对上游重试）；每步后跑 `test-platform.sh status`；
  3. 显式密钥轮换操作（1.2.1 后）：备份 → rotate → 立即跑 sync-sso-key.sh → audit/e2e 全绿 → 宣布；轮换记录进决策日志；
  4. 升级灰度：二进制先 staging 同版本验证，再服务器替换（capistrano current/shared 结构现成，回滚 = 切 previous）。
- **验收标准**：runbook 文档化并演练一次；演练中 `test-platform.sh e2e` 最大失败窗口 ≤2min（timer 同步前），1.2.1 落地后为 0。

## 5. 不接清单复核（克制原则）

第一轮 11 项不接清单**全部维持**（复核：当前状态无任何新信号要求翻案）：
aero-im 全量 IM（通知已按需接入，直播/SFU/审核不接）；vault RAG/MCP/WebDAV（直传已够用，触发=检索需求）；aero-id 账户生命周期（外部 SSO 是身份权威）；ERP 多租户数据模型（低成本预留保持：tenant 上下文由 claims 盖章注入、事件带组织上下文，不改表结构）；替换 outbox（触发=governance 提供事务性中继）；gRPC 直连（BFF 聚合已覆盖）；OTel 替代审计；Billing/插件市场；iris-ui 升级保持锁版本 0.2.x + 升级门禁（Alpha 期不追新）；**新增不接项（Proposed）**：snaplink AuditSink SSO 内部审计入账（触发=合规要求或 SSO 审计事故）、HTTPS+JWKS（触发=出内网/多主机/合规）、aero-im 跨主机凭据（NAT/TURN、FCM/APNs、SMTP/OIDC/OTLP——未验收不外推）、集中式 secrets 管理（触发=主机数>1）。每项触发条件已文档化，**不接清单本身是纪律而非欠账**。

## 6. 待人工批准项与 owner 决策

| # | 决策项 | owner（建议） | 门禁/验收 |
|---|---|---|---|
| 1 | 1.2.1 密钥轮换语义修复（snaplink 仓） | snaplink 仓 owner | 重启演练 + 单测 |
| 2 | 1.2.2 L2 宣布前置收尾（DEMO_PASSWORD/配置 fail-closed/HTTPS 豁免决策） | sverpweb 仓 owner + 平台 owner | L2 checklist 全勾 |
| 3 | 1.2.3 console 审计时间线（console 仓，跨仓契约走 Change Manifest） | console 仓 owner | 契约测试 + 审计页 E2E |
| 4 | 1.2.4 pi-batch 嵌入（sverpweb 仓） | sverpweb 仓 owner | platform-247 第 7 仓不 SKIP |
| 5 | 1.2.5 vault 迁移（sverpweb + vault 仓） | sverpweb 仓 owner + vault 仓 owner | 回填校验 + 双读切流 |
| 6 | 1.2.6 M5 通知扩展 | sverpweb 仓 owner（业务事件面确认先行） | 故障注入不阻塞 |
| 7 | 3.2 服务账号 scope 收窄（sso-admin/sverp-audit） | 平台 owner（snaplink 仓） | 负向 403 测试 |
| 8 | 4.2 全量密钥轮换 + 密钥清单表 | 平台 owner | keys.txt 更新 + 演练 |
| 9 | 4.3 备份 + 月度恢复演练 | 平台 owner（devops） | backup 子命令全绿 |
| 10 | 4.4 升级 runbook 演练 | 平台 owner | 演练记录 + 决策日志 |

**批准路径**：本文件为提案；owner 决策逐项人工批准后提升到 `docs/snaplink-platform/` 正式路线图；任一硬门禁（tsc 0、pnpm check、make ci、test-platform.sh 三套件）不豁免。**克制原则保持**：本评估不新增任何超出上述触发条件的能力清单项。
