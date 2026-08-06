# Security Engineer 评审 — 第二阶段路线图（phase2/kickoff.md，2026-08-06）

> 评审基线：本会话内直接阅读的源码与文档（snaplink `/home/dwp/snaplink`、snaplink-audit-governance、aero-vault、sverpweb-stable `~/workspace/job/sverpweb-stable`、DEPLOYMENT/TESTING_GUIDE/ERP_SCENARIO 三文档）。所有结论标注 **Verified**（本会话读码实证）/ **Partial**（证据部分）/ **Unknown**（需服务器现场核实）/ **Proposed**（推理建议）。本评审是提案，不替代任何仓库门禁。

---

## 1. 资产、信任边界、攻击者能力、入口点

### 1.1 资产清单（Verified，8 服务 :13013–13023）

| 资产 | 价值 | 关键保密/完整性属性 |
|---|---|---|
| snaplink-sverp (:13015/:13016) | 身份权威：admin/tester 口令、client secrets、签名私钥、JWT/refresh 签发 | 私钥泄露=全链伪造；口令=账号接管 |
| sverpweb-stable (:13014) | ERP BFF：业务数据、媒体、outbox、**5 组服务凭据**（audit OAuth、vault OAuth、IM 账号、snaplink client、session key） | BFF 失陷=凭据+业务全量 |
| audit-governance (:13018) | 审计事实源：state.json 全量事件 + Payload（业务数据） | 篡改=审计失效；泄露=业务数据出界 |
| aero-vault (:13019) | 媒体文件 + presign HMAC secret | 对象读写越权、presign URL 能力外泄 |
| aero-im (:13022) | 通知通道 + RSA-2048 签名密钥 + 自有账号 | 伪造通知/告警欺骗 |
| aero-id (:13020/:13021) | 账户域 PG + signing_key | 触发条件外不接入（H2），风险面未展开 |
| PG/Redis/NATS | 数据底座；PG 口令 dev 级（`aero_local_dev_only`） | 口令轮换未做（路线图 4.2 已列） |
| 服务器 keys.txt / accounts.txt / .env | sso-admin secret、admin 口令、sverp-audit secret、IM 口令 | 明文静态凭据集 |

### 1.2 信任边界

```
[浏览器] --HTTP--> [nginx/console :13017]          ┐
[浏览器] --HTTP--> [sverpweb BFF :13014] ──服务凭据──┤ 同一 LAN 网段（192.168.123.0/24）
[脚本/curl] ──HTTP──> [snaplink :13015] ◄──OAuth──┘  所有服务互信边界=HTTP 明文 + 共享 LAN
                          │ gRPC :13016
                          ▼
[governance :13018] ◄─sverp-audit/sso-admin Bearer──[vault :13019] [aero-im :13022] [aero-id]
```

- **边界 1（最关键）**：浏览器 ↔ 各服务 = HTTP 明文，无 TLS；LAN 段上任何能抓包的主机可嗅探口令/会话 cookie/Bearer。
- **边界 2**：BFF ↔ 下游服务 = 服务账号 OAuth，凭据仅存服务端（Verified 正向）；但 **BFF 不把 ERP 用户身份传递给 vault/governance**——下游只见服务账号租户（sverp），不见操作人角色。
- **边界 3**：governance/vault 对 snaplink 的信任 = 公钥静态注入（PEM/timer），签名密钥语义未受控（1.2.1 根因）。

### 1.3 攻击者能力模型（本内网部署）

| 能力 | 现实性 | 后果 |
|---|---|---|
| A1：LAN 抓包/ARP 欺骗 | 高（若网段共享） | 口令、cookie、Bearer 全量可嗅（F7） |
| A2：任一 ERP 低权账号（tester 级别） | 已存在 | 集成面 RBAC 绕过→审计读、媒体读写、IM 伪造（F1） |
| A3：服务器本地读文件 | 中（运维信任） | keys.txt/.env/state.json 明文（F6、F10） |
| A4：重启/运维操作触发 | 必然发生 | 全链认证失效窗口 + 吊销集合丢失（F2/F3） |
| A5：持有泄漏的 presign URL | 中（日志/粘贴） | 最长 7 天免认证读文件（F5） |

---

## 2. Findings（按严重度）

### F1 [High] — 集成测试面 RBAC 绕过：`/integrations/*` 全部路由仅 `requireSession`，无权限检查，与代码注释宣称的控制不符

- **证据（Verified）**：`apps/api/src/routes/integrations.ts` 头部注释声明"所有端点 requireSession + requirePermission("system:settings:read")"，但 8 条路由（L112 status / L138 upload / L170 files / L186 presign / L215 delete / L240 im message / L269 im messages / L285 audit events）**全部只挂 `authenticated`（requireSession）**；vault 侧 presign 签发只按服务账号租户 + 对象 ACL 校验（`aero-vault/internal/api/rest/presign.go` → `checkBucketPolicy` + `objectForAction`），**不感知 ERP 用户角色**。
- **利用前提与步骤**：
  1. 以 tester（质检最小 5 项权限）登录 ERP；
  2. `GET /api/integrations/vault/presign?key=<任意 sverp 租户对象 key>&op=get&expires=604800` → 得到任意媒体对象的能力 URL（浏览器可直接打开）；`op=put` 同理可写；
  3. `GET /api/integrations/audit/events` → 全量 sverp-web 审计时间线**含 Event.Payload 业务数据**（`governance models.go:43-68` 确认 query 返回完整事件含 payload）；
  4. `POST /api/integrations/im/message` → 向 "SVERP Integration" 频道发伪造质检通知（该频道正是 4.1 计划的告警 sink）。
- **影响**：tester 及任何未来低权账号 = 审计数据读取（跨角色业务泄露）+ sverp 租户媒体读写/覆盖（`vault handler.go:95-117` Put 无版本检查、无 If-Match 时直接覆盖已存在对象 → **数据完整性破坏**）+ 通知欺骗。与 TESTING_GUIDE"tester 仅 5 项权限"的宣称直接矛盾。
- **修复**：① 全部集成路由补 `requirePermission("system:settings:read")`（与注释一致），或按操作细分（vault→`media:read/write`、audit→`audit:event:read`）；② presign 的 `key` 参数做服务端归属/前缀校验（不允许任意 key）；③ 修复后跑 tester 负向矩阵。
- **回归测试**：tester 对各集成路由断言 403；admin 200。加入 test-platform.sh negative 子命令。
- **路线图关系**：**1.2.3/3.2 均未覆盖此漏点**——路线图只处理了 console 审计页与 sso-admin 过宽，现有集成页本身是已存在的审计读面 + 媒体写面。建议并入 1.2.3 前置或独立 P0 硬化项。

### F2 [High] — 1.2.1 密钥持久化修复将导致"已吊销令牌在重启后复活"（吊销集为纯内存）

- **证据（Verified）**：`snaplink/infrastructure/defaultimpl/ed25519_jwt_issuer.go:67,101-104` — 吊销 deny-set 纯进程内存；持久化需 `keys.signing.revocation_backend`（sqlite/redis/memory，`build_signing.go:74-94` 现成）；`cmd/sso-minimal/app.go:53` 未接线任何 revocation store。部署是否配置了 revocation_backend **Unknown（需服务器核实 systemd 环境）**。
- **机制**：今天"重启即轮换密钥"掩盖了此问题——重启后所有 token 本就不验。1.2.1 持久化密钥后，重启会**保留** token 的验证能力，但**丢失**吊销集 → 已撤销的 access/refresh token 全部复活。
- **影响**：被盗 token 在"吊销→重启"窗口后仍可用；"登出/撤销"承诺在重启后被打破——对 SSO 是实质性安全退化，恰好由路线图自己的修复引入。
- **修复**：1.2.1 必须**同时**配置 `keys.signing.revocation_backend: sqlite`（与持久密钥同一 DSN）+ SeedRevocations；验收标准增加：吊销 token → `systemctl restart snaplink-sverp` → 该 token 仍被拒。
- **回归测试**：snaplink 单测——revoke 后重启 issuer（重建 + seed），Validate 拒绝；sverpweb e2e——登出后重启，旧 refresh token 换新失败。
- **路线图关系**：1.2.1 验收只写"重启后三套件全绿且无需手工同步"，**缺吊销持久性断言**——必须补充。

### F3 [Medium-High] — 已知默认凭据风险 + 配置校验 fail-open（1.2.2 的 fail-closed 需扩展）

- **证据（Verified）**：`cmd/sso-minimal/config.go:14-23` — 默认用户 `alice`/`s3cret`、默认客户端 `demo-app`/`demo-secret`、`demo-app-b`/`demo-secret-b`，未设 `SSO_MINIMAL_*` 环境变量即生效；`sverpweb env.ts:61` — `IDENTITY_MODE` 除字面 `"demo"` 外任何值（含拼写错 `"Demo"`）静默落到 snaplink 模式（fail-open 方向为 demo→snaplink，属可用性而非认证弱化，但配置校验缺失本身是问题）。部署的 snaplink-sverp 实际使用 minimal 还是 sso-server 形态 **Unknown（需服务器核实）**。
- **影响**：若 systemd 单元未显式设置全部凭据环境变量，默认口令/secrets 在 :13015 上可用 → 直接账号接管。
- **修复**：1.2.2 的"配置 fail-closed"扩展为：生产标记（`NODE_ENV=production` 或 `PRODUCTION=1`）下**拒绝已知默认口令与默认 client secret**（对比默认清单，启动即拒并给出键名）；`IDENTITY_MODE` 严格枚举校验（demo 模式需显式 `IDENTITY_MODE=demo` + 生产标记冲突即拒）。
- **回归测试**：配置校验单测——默认口令 + 生产标记 → 启动失败；`grep -n "s3cret\|demo-secret"` 服务器 systemd 单元零命中（运维核查项）。

### F4 [Medium] — presign 能力 URL：有效期客户端可控（最长 7 天）+ key 客户端任意指定 + 可覆盖已存在对象

- **证据（Verified）**：`aero-vault/internal/api/rest/presign.go` — `expires` 取自客户端 query（默认 300s，上限 7 天，`presign_put.go:22 maxPutPresignTTL = 7*24h`）；BFF `integrations.ts:186-212` 把客户端 `key`/`expires` 原样透传，并把 URL 内部 host 重写为 publicBaseUrl（URL 在 LAN 内可直接使用）；`handler.go:95-117` Put 无 If-Match 时**直接覆盖已存在对象**；签名仅绑定 method+path+tenant+expires（`presign_put.go:133-166`，hmac.Equal 比较，主机名不参与签名——host 重写安全，但能力本身无持有者绑定）。
- **利用前提与步骤**：获得任一已认证 ERP 会话（F1 下 tester 即可）→ 请求 `expires=604800` 的 GET/PUT 能力 URL → 泄漏/粘贴 URL 者在 7 天内免认证读写该对象；PUT 可覆盖同租户任意 key 的既有对象。
- **影响**：泄漏窗口长（浏览器历史/代理日志/IM 粘贴均为现实渠道）；数据完整性（覆盖质检照片/单据附件）；F1 修复后此问题仍独立存在（任意持 URL 者）。
- **修复**：① BFF 钳制 `expires`（GET ≤ 15min，PUT ≤ 1h），服务端拒绝超限；② vault 对 presign PUT 的覆盖语义加前置检查（已存在对象须 If-Match 或确认语义）；③ presign 签发计入可观测性（现有 recordPresign 已记录——补告警阈值）。
- **回归测试**：`expires=999999` → 被钳制；过期 URL → 403；presign PUT 覆盖他人对象 → 409/403。
- **路线图关系**：3.2 只提"跨 tenant 403 测试"，**未提 TTL 钳制与覆盖语义**——补充进 1.2.5 验收标准。

### F5 [Medium] — 审计账本明文落盘（含业务 Payload），4.3 备份将复制明文

- **证据（Verified）**：`governance models.go:43-68` — Event.Payload 明文入账；`ERP_SCENARIO §1` — "EncryptedFields 能力现成未用"；`fieldcrypto.go` 加密原语现成；导出文件有独立 AES-GCM 加密（`server.go:451`）但活账本未加密。
- **影响**：服务器失陷或备份介质泄露 → 全量业务审计数据（质检/采购单据细节）明文可读。
- **修复**：对敏感 payload 字段启用 EncryptedFields（receipts/digest 不受影响，逐事件解密不影响哈希链——需一次兼容性验证）；4.3 备份机制增加"备份加密 + 0600 权限 + 恢复演练含解密验证"。
- **回归测试**：备份文件 `grep` 敏感字段零命中；恢复后查询全绿。
- **路线图关系**：4.3 未提备份加密——补充；EncryptedFields 启用列为 1.2.2/4.3 之间的低成本硬化项。

### F6 [Medium] — HTTP 明文承载 SSO 凭据；HTTPS 豁免的触发条件需补充"LAN 段共享不受信主机"

- **证据（Verified）**：全部服务纯 HTTP（:13013–:13023）；`env.ts:66` `COOKIE_SECURE` 默认 false；OIDC code 走 URL、口令走 HTTP POST、Bearer 走 Authorization 头——同一 LAN 段可全部嗅探（含 admin 口令）。
- **影响**：若 192.168.123.0/24 段出现不受信主机（访客 WiFi、其他 VM、ARP 欺骗、上游交换机镜像），admin 口令/会话 cookie 被抓包 = 完整账号接管；governance/vault 的 Bearer 被抓包 = 审计写/读 + 媒体读写。
- **修复**：维持豁免决策（低成本、内网），但把触发条件前置化：**"LAN 段出现不受信主机"即触发 nginx TLS 终结 + `cookieSecure=true`**，写入部署文档与 2.1 决策；4.4 runbook 增加 TLS 前置检查步骤。
- **回归测试**：TLS 启用演练（自签 + 固定）+ `COOKIE_SECURE=true` 断言 cookie 带 Secure 属性。
- **路线图关系**：2.1/1.2.2 豁免合理，触发条件清单不完整——补充此项。

### F7 [Low] — `/token` 无 client_credentials 限流/锁定（暴力枚举面）

- **证据（Partial）**：snaplink sqlite 侧有密码流账户锁定（`sqlite/account_lockout.go`）；client_credentials 无锁定。LAN 信任模型下风险低。
- **处置**：记入残余风险；若 F6 触发条件成立（暴露面扩大）则在 nginx 层限流 `/token`。

### F8 [Low] — demo 身份模式零认证（可伪造登录）；1.2.2 的"demo 禁用"应针对 `identityMode` 本身而非仅 DEMO_PASSWORD

- **证据（Verified）**：`sverpweb apps/api/src/auth/demo.ts` — code 仅为 `demo-code.<base64url(username)>`，**无任何口令校验**；DEMO_PASSWORD 是死配置（`.env:9` 存在但 `env.ts` 从不读取——路线图 1.2.2 的 Verified 判断正确）。demo 模式为显式 opt-in（`IDENTITY_MODE=demo`），LAN 演示可接受。
- **修复**：1.2.2 验收增加 `IDENTITY_MODE=snaplink` 断言 + 生产标记下拒绝 demo 模式（与 F3 同一配置校验）；DEMO_PASSWORD 清除照做但注意它只是噪音，**真风险是 demo 身份客户端本身**。

### F9 [Info] — 密钥清单缺口：`AUTH_PRESIGN_SECRET` 与 BFF IM 凭据未入 4.2 清单

- **证据（Verified）**：`aero-vault config.go:205` — `AUTH_PRESIGN_SECRET` 空 = 进程随机密钥 → **vault 重启使全部在途 presign URL 失效**（可用性）；BFF `.env` 明文存 `INTEGRATION_IM_EMAIL/PASSWORD`（IM 自有账号）。
- **修复**：4.2 密钥清单补两项：AUTH_PRESIGN_SECRET（设置 32+ 字节并轮换，随 1.2.5 上线）、IM 账号口令（随 4.2 全量轮换）。

---

## 3. Abuse-case 表（STRIDE 映射）

| 威胁 | 场景 | 现状 | 结论 |
|---|---|---|---|
| **身份伪造 Spoofing** | demo 模式伪造 `demo-code.<user>` 登录 | demo.ts 零口令校验（opt-in） | 部分保护 → F8 |
| | 默认凭据 `alice/s3cret`、`demo-app/demo-secret` 存活 | minimal 构建默认值存在，部署形态 Unknown | 缺口 → F3 |
| | 伪造 sverp 签名 token（需私钥） | EdDSA 私钥进程内随机生成，无外部注入面；重启即换 | Protected（恢复期私有） |
| **重放 Replay** | 审计事件重投 | 202+Duplicate 幂等 + idempotency_key=event.id（Verified，契约测试） | Protected |
| | presign URL 重放 | 能力语义 + 7 天上限；无持有者绑定 | 部分保护 → F4 |
| | refresh token 重放 | `config_identity.go` 声明 RFC 6749 §6 单次使用（Partial，未在本会话验证 minimal 构建） | Unknown → 补测试 |
| | 已吊销 token 重放（重启后） | 吊销集纯内存 + 密钥持久化计划 = **复活窗口** | 缺口 → F2 |
| **跨租户 Cross-tenant** | presign 跨 tenant 读/写 | 签名绑定 tenant + 签发时 ACL 校验（Verified）；**无跨 tenant 403 实证测试**（路线图 3.2 已列） | 部分保护 → 补负向测试 |
| | governance 跨 tenant 查询 | `tenantFor` 仅 platform claims 可指定 tenant_id；sverp-audit 需 cross_tenant scope（Verified 门禁） | Protected |
| | BFF 集成面任意 key 访问 | vault 只按服务账号租户校验，不感知 ERP 用户 → 角色级越权 | 缺口 → F1 |
| **代理/头伪造 Proxy forgery** | presign URL host 重写 | 签名仅覆盖 method+path+tenant+expires，host 不参与 → 重写安全（Verified） | Protected |
| | X-Forwarded-* 信任 | 未核实各服务是否信任代理头 | Unknown → 验证计划 |
| **资源耗尽 Exhaustion** | 审计查询 page_size | 上限 1000 + from/to 必填（Verified） | Protected |
| | presign 签发洪泛 | 每签发 = HMAC + 存储查找（廉价）；无速率限制 | 部分保护（LAN）→ F7 |
| | IM 频道洪泛（告警 sink 欺骗/淹没） | F1 下任意认证用户可 POST | 缺口 → F1 |
| | 导出任务 | `audit:export:create` scope 门禁（Verified，sverp-audit 收窄后不含） | Protected |
| **敏感数据泄露 Leakage** | 审计 Payload 读面 | F1 集成路由 = 任意认证用户可读全量 payload | 缺口 → F1 |
| | 账本/备份明文 | state.json 明文 + 备份无加密 | 缺口 → F5 |
| | 线上嗅探 | HTTP 明文 + cookie 非 Secure | 缺口 → F6 |
| | 服务器静态文件 | keys.txt/accounts.txt/.env 明文（0600） | 路线图 4.2 已覆盖（补充 F9） |

---

## 4. 正向控制已验证（本会话读码实证）

1. **governance fail-closed 认证**（Verified）：`ValidateConfiguration` 启动即校验；`AUDIT_ALLOW_DEV_AUTH=false`；HS256 需显式开关且与公钥互斥（`verifier.go:110-127`）；alg 白名单 + 本地 PEM 算法必须匹配；JWKS 拉取有受限 HTTP client + kid 唯一校验（`verifier.go:298-330`）。
2. **governance 查询边界**（Verified）：from/to 必填、from<to、page_size ≤1000；导出需 `audit:export:create` 且导出文件独立 AES-GCM 加密；错误分类 401/403/400/409 语义明确（oracle-safe）。
3. **vault presign 签发期校验**（Verified）：`checkBucketPolicy` + `objectForAction`（ACL/存在性）在签发时执行；签名覆盖 method+path+tenant+expires，hmac.Equal 常量时间比较；TTL 有 7 天上限。
4. **sverpweb 审计链路**（Verified）：outbox 先写后投递、permanent 毒丸隔离、逐事件 receipts、幂等键=event.id；OAuth token provider 带 exp 缓存 + 提前 60s 刷新 + 401 强制刷新（`oauth-token.ts`）；变更类 BFF 路由有 CSRF；服务凭据仅存服务端。
5. **snaplink 签名面**（Verified）：JWT alg 白名单（仅 EdDSA）+ typ 白名单（`ed25519_jwt_issuer.go`）；时钟偏差有界；可选持久吊销存储现成（待接线，见 F2）。
6. **权限边界实证**（Verified，引用路线图/测试手册）：tester 403 边界实测；governance 无 token 401 门禁实测；读侧 Bearer + policy scope 门禁实测。

## 5. 残余风险（评审后仍需现场核实）

| 项 | 状态 | 核实动作 |
|---|---|---|
| snaplink-sverp 部署形态（minimal vs sso-server）与默认凭据是否存活 | Unknown | `systemctl cat snaplink-sverp` + 环境变量清单 |
| `keys.signing.revocation_backend` 是否配置 | Unknown | 同上（F2 依赖） |
| PG/Redis 监听地址与 pg_hba（`aero_local_dev_only` 是否 LAN 可达） | Unknown | `ss -lntp` + `pg_hba.conf` |
| keys.txt/accounts.txt 实际权限与在途轮换状态 | Unknown | `ls -l`（路线图 4.2 已列） |
| 各服务是否信任 X-Forwarded-*（nginx 前置 console） | Unknown | 代码核查 console 仓 |
| presign PUT 覆盖已存在对象的 ACL 复核 | Partial | 补 403/409 负向测试（F4） |
| refresh token 单次使用在部署形态下的实证 | Partial | 补 e2e 重放测试 |
| snaplink AuditSink（SSO 内部事件入账） | 未验证 | 路线图 3.1 暂缓决策**同意**（触发条件已文档化） |
| console LocalStorage 本地审计记录残留 | 未验证 | 1.2.3 迁移后应显式清理，验收加"无本地业务数据副本" |

## 6. 优先验证计划（与里程碑绑定）

**立即（人工核查，半天级，先于 1.2.1 实施）**
1. `systemctl cat snaplink-sverp aero-vault` → 默认凭据/revocation_backend/AUTH_PRESIGN_SECRET 三项核实；
2. `ss -lntp | grep -E '5432|6379'` + pg_hba → PG/Redis 暴露面；
3. F1 手工复现：tester 会话调 `vault/presign` 与 `audit/events`，确认 200（预期应 403）。

**随里程碑（回归测试写入 test-platform.sh negative 子命令）**
- F1 → 1.2.3 前置：tester 对全部集成路由 403 矩阵；
- F2 → 1.2.1 验收追加：吊销 → 重启 → token 仍被拒；
- F3/F8 → 1.2.2 验收追加：默认凭据拒绝 + `IDENTITY_MODE` 严格枚举 + 生产拒绝 demo 模式；
- F4 → 1.2.5/3.2 验收追加：expires 钳制、过期 403、presign PUT 覆盖 409；
- F5 → 4.3 验收追加：备份加密 + 敏感字段零命中；
- F6 → 2.1 决策补充触发条件 + 4.4 runbook TLS 前置检查；
- F9 → 4.2 密钥清单补两项。

**裁决意见（Proposed）**：路线图主体方向正确（1.2.1 根因、1.2.2 fail-closed、3.2 最小权限、4.2/4.3 密钥备份），但存在 **1 项高危漏点（F1 集成面 RBAC 绕过，先于 1.2.3 修复）**、**1 项由修复本身引入的安全退化（F2 吊销复活，必须并入 1.2.1 验收）**，以及 4 项需并入现有里程碑验收标准的 Medium 项（F3–F6）。在 F1/F2 落地前，不建议宣布 L2 或把集成页作为唯一审计读面扩展。
