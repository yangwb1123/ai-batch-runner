# Identity Protocol Expert 评审 — 第二阶段路线图（phase2/kickoff.md，2026-08-06）

> 评审基线：本会话内直接阅读的源码与 OpenAPI（snaplink `d19838d8`、sverpweb-stable
> `abd8801`、snaplink-audit-governance `476d083`、aero-vault `c0ce6db`、aero-id `9a4ec42`、
> snaplink-console `0c21dca`）与 `docs/openapi.yaml`（18219 行）、DEPLOYMENT/TESTING_GUIDE/
> ERP_SCENARIO 三文档、kickoff 正文（与 `/tmp/kickoff-final.md` 一致）。所有结论标注
> **Verified**（读码/读文档实证）/ **Partial**（证据部分）/ **Unknown**（需服务器现场核实）/
> **Proposed**（推理建议）。本评审是提案，不替代任何仓库门禁；**不宣称任何 OIDF 认证**。
>
> 评审对象是**已部署协议面**（snaplink-sverp :13015/:13016 的 sso-server stock 二进制 +
> sverpweb BFF 作为 OIDC RP + governance/vault/aero-id 作为 RS 消费方 + sso-minimal
> 原型 edition），对照协议条款逐项核验 handler、discovery 元数据、OpenAPI 与测试。

---

## 1. Protocol/profile scope 与权威引用

### 1.1 实际生效的协议面（Verified，按代码挂载）

| 面 | 标准 | 挂载证据 |
|---|---|---|
| 授权码流 + PKCE | RFC 6749 §4.1 / RFC 7636 | `interfaces/sso/server_login.go`、`server_finish_login.go`（OAuth 2.1 strict 下强制 S256）、`internal/handler/tokengrant/token_authcode.go` |
| Token 端点（含 client_credentials / refresh / token-exchange / device） | RFC 6749 §3.2/§4.4/§6、RFC 8693、RFC 8628 | `interfaces/sso/server_token.go` dispatch 分支；`server_routes.go:184` device 挂载 |
| Bearer 资源访问 | RFC 6750 §2/§3.1 | `protocols/oidc/handle_userinfo.go`（BearerToken + challenge + `invalid_token`） |
| JWT Access Token（RFC 9068 profile） | RFC 9068 §2.1/§2.2/§4 | `infrastructure/defaultimpl/ed25519_issue.go`（`typ: at+jwt`、jti）、`issue_payload.go`（scope/client_id/auth_time/amr/acr/sid/cnf/aud） |
| JWT / SAML Bearer 断言 | RFC 7521/7523/7522 | `server_token_clientauth.go`（private_key_jwt）、`JWTBearerAssertionValidator`/`SAML2AssertionValidator` |
| 撤销 / 内省 | RFC 7009 / RFC 7662 | `protocols/oauth/handle_revoke.go`、`handle_introspect.go`；BFF 实测 `postRevoke`（snaplink.ts:274） |
| 元数据 / Discovery | OIDC Discovery 1.0 + RFC 8414 §2/§3 | `server_discovery_config.go`（两路径同 handler）、`cmd/sso-minimal/surface.go`（edition 收窄） |
| `iss` 参数 | RFC 9207 | `server_finish_login.go`（`KeyIss` 进 authz 响应）、`AuthorizationResponseIssParameterSupported=true`；RP 校验（service.ts:122-128） |
| DPoP / mTLS | RFC 9449 / RFC 8705 | `captureSenderConstraint`、`captureAuthCodeDPoPBinding`（RFC 9449 §10 code 绑定）、`authenticateMTLSClient` |
| 资源指示符 | RFC 8707 | `client.AreResourcesAllowed` + `aud` claim（issue_payload.go:104-110） |
| 刷新令牌轮换 + 重用检测 | RFC 9700（OAuth Security BCP）§4.13 之上 | `internal/handler/tokengrant/token_refresh.go`（family kill/velocity cap/绝对最长寿命） |
| OIDC Core | Core 1.0 §2/§3.1.2.1/§3.1.3.7/§5.3/§5.4/§5.5 | `protocols/oidc/handle_userinfo.go`、`handle_silent_renewal.go`、id_token 签发（ed25519_issue.go:70+） |
| RP-Initiated Logout / Session Mgmt | OIDC RP-Initiated Logout 1.0 / Session Mgmt 1.0 §2 | `handle_end_session.go`、`applySessionManagement`（`session_state` + `check_session_iframe`，opt-in） |
| JAR / PAR / JARM | RFC 9101 / RFC 9126 / JARM | `handle_par.go`、`handle_jar`、`SignJARMResponse`（仅接线时生效，minimal 全部隐藏） |

### 1.2 已部署形态与 profile 判定

- **已部署 = sso-server stock 二进制 + minimal 配置（sqlite+JWT）**（DEPLOYMENT §1：`snaplink-sverp.service`，
  minimal（sqlite+JWT），:13015 HTTP / :13016 gRPC）。**服务器侧实际配置不可见**（systemd 单元/env/自定义
  config.yaml 均 Unknown——与 security 评审 F3 同口径）。
- **stock config.yaml 默认 `oauth_21_strict_mode: false`（Verified，config.yaml:74）**；sso-minimal
  edition 强制 `WithOAuth21StrictMode(true)`（app.go:61）。因此**已部署 profile 是 OAuth 2.0 而非
  OAuth 2.1**：plain PKCE、非 https 重定向 URI、非 `code` response_type 均可按配置存活（见 F4）。
- sso-server stock 还挂载：SCIM 路由（`/api/v1/scim/*`，`build_http.go:331`，权限门禁）、WebAuthn
  （`serverwebauthn.MountWebAuthnRoutes`，build_http.go:293）、device flow（server_routes.go:184）。
  SAML 2.0 SP 面**不 stock 接线**（`saml.handler` 需 fork 注册，build_http.go:372-398）。minimal
  edition 的 discovery 显式隐藏 PAR/registration/revocation/introspection/device/backchannel（surface.go）。
- **OIDC 门**：minimal edition `oidcEnabled=true`（edition.go:36）→ userinfo/end_session/discovery
  挂载；sso-server 经 `feature_gates.oidc` 热开关（mountOIDCUserEndpoints 用 GatedRouter）。

---

## 2. Compliance matrix（节选，按 severity 相关度排序）

> 状态：✅ Verified 合规 / ⚠️ 部分合规或依赖配置 / ❌ 不合规（deviant）/ ⬜ 未接线（声明不支持）

### 2.1 授权端点与 code 流

| 条款 | 要求 | 实现证据 | 状态 | 偏差 | 测试 |
|---|---|---|---|---|---|
| RFC 6749 §3.1.2.3 / OAuth 2.1 §4.1.3 | redirect_uri 精确匹配注册表；strict 下仅 https/loopback http | `core/types.go:368 IsRedirectURIValid`（`slices.Contains` 精确匹配）；`server_finish_login.go:394` strict 强制 `isSecureRedirectURI`（https \| localhost/127.0.0.1/::1，oauthwire/auth_code_handler.go:30） | ✅（strict 开时）；⚠️（部署 strict=off 时 http LAN 可注册，见 F4） | 部署面 http://192.168.123.52:13014/callback 属于非 loopback http | sso-minimal flow_test.go `TestAuthorizationCodePKCEFlow`；rootcov 错误码测试 |
| RFC 7636 §4.1/§4.3 | verifier 43–128 字符；S256 强推；常量时间比较 | `consts_oauth.go:119-120`（43/128 边界）；`VerifyPKCE`（S256 sha256+RawURLEncoding+`subtle.ConstantTimeCompare`）；`server_finish_login.go:350-382`（strict 拒 plain；per-client allowlist） | ✅ | — | flow_test `TestPKCEIsRequired`；oauthwire fuzz/bind 测试 |
| RFC 6749 §4.1.2 | code 单次使用、短 TTL、随机 | `GenerateAuthCodeBytes`（32 字节 CSPRNG）；`AuthCodeStore.Consume` 原子删除；minimal TTL 5min（app.go:31） | ✅ | — | rootcov `TestRcov_AuthCodeRoundTrip` |
| RFC 6749 §4.1.3 / §5.2 | code 换 token：client 绑定 + redirect_uri 绑定 + PKCE；失败统一 `invalid_grant`（oracle 收敛） | `token_authcode.go authCodeValidate`：未知/过期/已消费 code、client 不匹配、redirect_uri 不匹配、PKCE 失败、DPoP jkt 不匹配**全部 400 invalid_grant** | ✅ | — | rootcov `TestRcov_AuthCodeCannotExpandEmptyGrant` |
| RFC 9207 §3 | 授权响应携带 `iss` | `renderAuthCodeResponse`（`KeyIss`）；discovery 声明 `authorization_response_iss_parameter_supported: true` | ✅ | — | RP 侧 service.test.ts `TestAuthService...issuer` |
| RFC 6749 §5.1 | token 响应 Cache-Control no-store | `server_token.go:31-36`（成功与错误统一先置头）；`/auth/login` 同（server_login.go:26-30） | ✅ | — | rootcov |
| OAuth 2.1 §1.1 | response_type 限 `code` | strict 下 `server_finish_login.go:82` 拒非 code | ✅（strict） | 部署 off | surface_test |

### 2.2 Token 端点与客户端认证

| 条款 | 要求 | 实现证据 | 状态 | 偏差 | 测试 |
|---|---|---|---|---|---|
| RFC 6749 §2.3.1/§3.2.1 | client_secret_basic/post；错误 `invalid_client` 401 | `server_token_clientauth.go`：`inspectTokenClientAuth`（Basic 头优先，body 二义性拒绝）；per-client `TokenEndpointAuthMethod` 契约（声明的方法不可回退）；`ValidateSecret` | ✅ | 传输层 HTTP 明文（F3） | rootcov `TestRcov_TokenClientAuthPreservesBasicPrecedence`、`...RejectsAmbiguousOrMalformedCredentials` |
| RFC 7523 §2.2 | private_key_jwt 断言认证 | `verifyTokenClientAuth`（kind==privateKeyJWT 直达）+ `VerifyJWTClientAssertion` | ✅ | — | workload_identity_clientauth_test |
| RFC 6749 §4.4 | client_credentials 仅限机密客户端；scope 按 allowlist | `denyPublicClientCredentials`；`GrantedScopes`（allowlist 外 → 400 invalid_scope）；sub=client_id | ✅ | — | rootcov `TestRcov_ClientCredentialsGrant`；minimal `TestPrototypeRejectsClientCredentialsGrant` |
| RFC 6749 §6 | refresh 不扩 scope、client 绑定 | `IsScopeSubset`（token_refresh.go）；`info.ClientID != client.ID → invalid_grant` | ✅ | — | rootcov `TestRcov_RefreshGrant` |
| RFC 9700 §4.13+ | 刷新令牌轮换 + 重用检测 | family kill（重用即杀全族）、rotation velocity cap、绝对最长寿命、`RecordRefreshTokenReuse` | ✅（超集实现） | — | refresh_grace_test、rootcov |
| RFC 6749 §5.2 | 错误码语义（invalid_request/invalid_client/invalid_grant/unauthorized_client/unsupported_grant_type/invalid_scope/access_denied） | dispatch switch + `errorBody`；未知 grant → `unsupported_grant_type` 400 | ✅ | — | rootcov `TestRcov_TokenUnknownGrant` |

### 2.3 Token 形态与 RS 侧校验

| 条款 | 要求 | 实现证据 | 状态 | 偏差 | 测试 |
|---|---|---|---|---|---|
| RFC 9068 §2.1 | header `typ: at+jwt` | `ed25519_issue.go:54`（`jwtTypAT`） | ✅ | — | rs 侧 typ gate（validate.go `checkAccessTokenTyp`，大小写不敏感） |
| RFC 9068 §2.2 | jti/scope/client_id/iat/exp/nbf；可选 auth_time/amr/acr/sid/cnf/aud | `issue_payload.go buildAccessPayload` + `applyOptionalClaims` | ✅ | `aud` 仅资源指示符时出现（RFC 8707 语义，RS 需自配 audience） | aud_claim_fuzz_test |
| RFC 9068 §4 | RS 校验：typ、kid 选定 key、alg 白名单、iss/exp/aud | `interfaces/ssoclient/rs/validate.go`（kid→唯一 key；alg allowlist 在 VerifyCompactJWS 内）；governance `verifier.go`（PEM 算法必须匹配、kid 唯一、JWKS 受限 HTTP client + 大小上限、HS256 显式 opt-in 且与公钥互斥） | ✅ | — | governance 契约测试、vault e2e |
| RFC 9449 §4/§8/§10 | DPoP 证明验证、nonce、code 绑定 | `verifyDPoPProof`（jti 重放存储、时钟偏差、nonce challenge `use_dpop_nonce`）；code 绑定在 `token_authcode.go`（jkt 不匹配 → invalid_grant） | ✅（opt-in per-request；RP 未用） | 部署客户端全 bearer，DPoP 未启用（F11） | dpop_test/dpop_replay_test |

### 2.4 OIDC 面

| 条款 | 要求 | 实现证据 | 状态 | 偏差 | 测试 |
|---|---|---|---|---|---|
| OIDC Core §2 | id_token 必备 iss/sub/aud/exp/iat；可选 nonce/auth_time/amr/acr/azp/sid | `ed25519_issue.go IssueIDToken`（必备自动盖章；可选非零才投影）；auth_time 取真实登录时刻（§3.1.3.7 语义，refresh 链不重置） | ✅ | — | sso-minimal flow_test（id_token 存在性） |
| OIDC Core §3.1.2.1 | prompt=none 静默续期；login_required 语义；id_token_hint 校验 | `handle_silent_renewal.go`（§3.1.2.6 三条检查：缺失/无会话/subject 不匹配 → login_required）；minimal flow_test `TestOPSessionSupportsPromptNoneAcrossRPs` | ✅ | — | flow_test |
| OIDC Core §3.1.3.7 | **RP MUST 验证 id_token**（签名/iss/aud/exp/nonce） | sverpweb `service.ts:173,212` **只存储不验证**；apps/api 无任何 JWT 库 | ❌ **Deviation（F1）** | id_token 收到即弃；无签名/iss/aud/exp/nonce 校验 | 无（缺口） |
| OIDC Core §5.3 | userinfo：Bearer 校验、sub 一致、可选签名响应 | `handle_userinfo.go`（RFC 6750 challenge；`MaybeSignUserInfo` 接线时签名）；openid scope 才投影 OIDC 形态（§5.4） | ✅ | — | rootcov `TestRcov_Userinfo`；BFF snaplink.test.ts |
| OIDC Core §5.5 | claims 参数投影 | code 流捕获 `RequestedClaims` → token 内 `_claims_` → id_token/userinfo 投影（token_authcode.go、userinfo） | ✅ | — | claims_param_test |
| OIDC Discovery §3 / RFC 8414 §2 | 文档字段与挂载路由一致；signed_metadata 可选 | `buildOIDCConfiguration`（字段由实际 wiring 推导：grant/response_type/PKCE 方法/端点 auth 方法/end_session）；RFC 8414 §2.1 signed_metadata 实现；ETag/If-None-Match + base-URL 键缓存 | ✅ | minimal edition 收窄隐藏未挂载端点（surface.go narrowMetadata）——**与真实路由一致** | surface_test `TestDiscoveryMatchesConfiguredPrototypeClients`；rootcov_discovery_test |
| RP-Initiated Logout §2 | post_logout_redirect_uri 白名单 | `IsPostLogoutRedirectURIValid`（types.go:376） | ✅ | — | rootcov `TestRcov_EndSession` |

### 2.5 元数据/OpenAPI/测试一致性

| 检查 | 证据 | 状态 |
|---|---|---|
| OpenAPI 与 handler 一致 | `docs/openapi.yaml`（18219 行）覆盖 /auth/login、/token（六类 grant 表）、/userinfo、/end_session、/.well-known/*；与代码分支逐条对应（如 agent delegation grant 标记 opt-in） | ✅（抽查一致；未做自动化 diff） |
| discovery 与路由一致（minimal） | surface.go `narrowMetadata` 删隐藏端点、`editionRouteAllowed` 404 未选路由；`TestPrototypeHidesUnselectedRoutes` | ✅ |
| 部署面 discovery 与路由一致（sso-server） | 文档字段从 wiring 推导（applyGrantEndpoints 等），无独立手写清单 | ✅（但部署 config 不可见 → 最终文档需服务器抓取核对，Unknown） |
| 测试体系 | snaplink rootcov 流测试 ~30 项 + minimal flow/surface + oauthwire fuzz；sverpweb snaplink.test/e2e、service.test（state/iss 单次消费）；governance 契约对拍 | ✅（覆盖面好，缺口见 §4） |

---

## 3. Findings（按 severity）

### F1 [High] — RP（sverpweb BFF）对 id_token 零验证，违反 OIDC Core §3.1.3.7

- **条款/等级**：OIDC Core 1.0 §3.1.3.7（MUST）。OIDC 客户端 profile 要求。
- **证据（Verified）**：`apps/api/src/auth/service.ts:173,212` 把 `id_token` 存入 session 后**从未**做
  签名/iss/aud/exp/nonce 校验；`apps/api` 无任何 JWT 校验依赖（package.json 无 jose/jsonwebtoken）；
  token 响应 schema 仅 `z.string().min(1)`（snaplink.ts:31）。RP 身份完全来自 `/userinfo`（Bearer），
  id_token 是死数据。
- **影响**：当前数据流（BFF 自己换 code、自己持 access token 调 userinfo）下无直接利用面；但：①
  一旦未来任何路径（如静默续期、`session.idToken` 被前端消费）信任 idToken，即无防伪能力；②
  `iss` 虽校验（service.ts:122），但**无签名校验的 token 可以被任何能伪造 JWT 的中介替换**——HTTP
  明文 LAN 下这是现实威胁（结合 F3）；③ 与 `scope: openid` 的宣称不符（要了 openid 却不用 id_token）。
- **纠正行为**：二选一——(a) 用 snaplink JWKS 校验 id_token（EdDSA、iss=baseUrl、aud=client_id、
  exp、nonce 若发送）后丢弃/落库；(b) 请求 scope 去掉 openid（或配置不签发 id_token），并删除
  session.idToken 存储。推荐 (a) + 增加 nonce（OIDC Core §3.1.3.7 要求发送了 nonce 就必须校验）。
- **验证步骤**：新增 BFF 单测——构造伪造签名 id_token 与错误 aud/iss 的 id_token 各一枚，断言
  createSession 拒绝；e2e 里断言 token 响应中 id_token 可被 JWKS 验证。

### F2 [High] — 密钥轮换语义（重启即换 key）与 RS 缓存/静态注入的协议一致性窗口

- **条款/等级**：RFC 7517/RFC 9068 §4（kid 换 key 语义）、RFC 9700 §4.7（密钥管理）。运营 profile。
- **证据（Verified）**：`ed25519_jwt_issuer.go` 私钥进程内随机生成，`rotation.enabled=false` 只关调度
  循环（DB architect C2 同源结论）；DEPLOYMENT §6 实测 kid 变化（`9WQoSnawsm8 → gwvKK02OO-g`）；
  governance 用 PEM 静态注入 + 2min timer（jwx 不支持 JWK JSON）；vault JWKSCache 轮询刷新；
  AS 侧 JWKS 体缓存 TTL 默认 5s。
- **影响**：每次重启 = 全链路短暂认证失效窗口 + 审计投递 401；kid 轮换若不在 RS 缓存刷新间隔内
  收敛，会产生可预测的拒绝窗口；与 1.2.1（密钥持久化）直接相关。
- **纠正行为**：采纳 1.2.1（load-or-generate 持久密钥 + 显式 rotate 命令）；**协议验收补充**：
  rotate 后同一 kid 旧 token 在旧 RS 缓存 TTL 内仍可验（宽限期），新 token 立即可用——写为
  JWKS 收敛断言（vault ≤ 刷新间隔、governance ≤ 2min timer）；rotate 输出新旧 kid 并记入决策日志。
- **验证步骤**：`rotate` 后断言 `/.well-known/jwks.json` 同时含新旧 kid（重叠期）→ 各 RS 切到新
  kid 后旧 kid 摘除；重启演练全绿且无手工同步（1.2.1 验收原样保留，追加 JWKS 收敛断言）。

### F3 [Medium] — 全部协议端点走明文 HTTP：Basic 客户端认证、refresh token、code 均在 LAN 明文

- **条款/等级**：RFC 6749 §2.3.1（secret 传输须 TLS 语义）、RFC 9700 §3.4（TLS 强制）、RFC 6750 §5。
  安全 profile 偏差。
- **证据（Verified）**：BFF SDK 用 `Authorization: Basic` 发送 client secret（client.ts:1877-1882）；
  refresh token 以 body 明文发送；授权码经浏览器 URL 重定向（BFF `/auth/callback?code=...`）；
  `allowInsecureHttpForDevelopment` 已启用（sverp-web redirect 是 http://192.168.123.52:13014）。
- **影响**：LAN 段任意抓包者可获 client secret（→ 冒充 sverp-web 换 token）、refresh token（→ 会话
  劫持）。**已缓解层**：PKCE S256 使偷到的 code 无用（verifier 在 BFF 内存流 store，不进浏览器）；
  state 防回调 CSRF；refresh 轮换 + 重用杀族使偷到的旧 refresh 可被发现。残余风险 = 活跃 refresh
  token 与 client secret 泄露。
- **纠正行为**：① 维持豁免但触发条件前置（security F6 一致）：LAN 出现不受信主机即 nginx TLS 终结；
  ② **低成本先行**：`COOKIE_SECURE` 无关此面，但可立即把 BFF→snaplink 的服务间调用改为
  client_secret_post 不行（同样明文）——**真正低成本项是 1.2.1 落地后加短 TTL + 服务账号 secret
  30 天轮换（4.2）**；③ 4.4 runbook 增加 TLS 前置检查步骤。
- **验证步骤**：抓包验证（tcpdump :13014/:13015）确认无 secret 明文（TLS 后）；或作为豁免决策
  写入部署文档并列出触发条件。

### F4 [Medium] — 部署 profile 实为 OAuth 2.0（strict off），与 minimal 原型（strict on）语义不一致

- **条款/等级**：OAuth 2.1 draft-ietf-oauth-v2-1（profile 选择）；RFC 7636 §4.3（S256 推荐）。
- **证据（Verified）**：`cmd/sso-server/config.yaml:74 oauth_21_strict_mode: false`；minimal edition
  强制 true。strict off 时：plain PKCE 可用（若 client 未设 allowlist）、非 https redirect 可注册、
  response_type 不限于 code。
- **影响**：两 edition 行为分叉，运维/测试在 minimal 上验证的 PKCE/redirect 语义在部署面可能不
  生效；`RequirePKCE`/`AllowedPKCEMethods` 在部署面未确认（Unknown）。
- **纠正行为**：显式决策并文档化——部署面开启 `oauth_21_strict_mode: true`（sverp-web 已 S256 +
  https 语义就绪，仅 redirect http 需豁免项或接受失败）；或明确声明"部署 profile = OAuth 2.0 +
  PKCE S256 强制（client 级）"，并加配置校验（启动即检查 sverp-web 的 RequirePKCE/AllowedPKCEMethods）。
- **验证步骤**：`curl -s https?://192.168.123.52:13015/.well-known/openid-configuration` 核对
  `code_challenge_methods_supported` 与 `response_types_supported`；tester 级负向：plain PKCE
  请求 → 若 strict 应 invalid_request。

### F5 [Medium] — 访问令牌不携带 tenant 上下文；RS 各自维护 client→tenant 映射（漂移面）

- **条款/等级**：RFC 9068 §2.2（可选 claims 投影）；跨组件互操作一致性。
- **证据（Verified）**：`ed25519_types.go` payload 无 `tenant_id`/`tenant` claim（grep 全仓仅
  audit 内部/管理 API 使用该键）；governance 靠源注册的 client 绑定解析租户
  （`service/sources.go:91 resolveIngestTenant`），platform token 靠 query `tenant_id`
  （server.go:824）；vault 靠 `clientTenants` 映射，其 `ten`/`tenant_id` claim 回退路径
  （snaplink.go:96-106）读取 snaplink **永不签发** 的 claim——死路径（空 → "no tenant mapping"
  fail-closed，安全但误导）。
- **影响**：token 离开 snaplink 后租户语义不透明；新增 RS 若照 vault 文档配置 `TenantClaim=ten`
  将全部拒绝；映射表与 client 注册之间无一致性校验，client 迁移/重命名可静默错租户。
- **纠正行为**：① 文档化"租户上下文在 token 外，由各 RS 的 client→tenant 映射解析"；② 或在
  snaplink 签发时投影 `tenant_id` claim（低成本，client_credentials 的 Subject.TenantID 已存在），
  统一 RS 读取路径；③ 删除/标注 vault 的 `ten` 死路径。
- **验证步骤**：负向测试——未注册 client 的 token 调 governance 事件写 → 拒绝（source 绑定
  校验已有，补 client 未绑定场景断言）；vault 跨 tenant 403（3.2 已列）。

### F6 [Medium] — sso-server stock 默认配置与 minimal 的安全基线不一致（HS256/audit dev-auth/默认凭据）

- **条款/等级**：RFC 9700（默认安全配置）；OIDC 信任面。
- **证据（Verified）**：stock `config.yaml` 审计/身份后端默认 memory；DB architect 复核
  "sso-server config.yaml identity/oauth/audit=memory、audit-api dev-auth 默认 true——部署层已
  覆盖"；minimal 默认凭据 `alice/s3cret`、`demo-app/demo-secret`（config.go:14-23，security F3
  同源）。部署面已用 `AUDIT_ALLOW_DEV_AUTH=false` + 重置凭据（DEPLOYMENT §2/§6）。
- **影响**：任何按 stock 默认起服的实例 = 默认 client secret 可认证 + 开发面开放；协议面影响 =
  默认 client 存在即伪造 token 面。
- **纠正行为**：采纳 1.2.2 的 fail-closed 扩展（生产标记下拒绝已知默认凭据，F3 已列）；
  密钥轮换（4.2）立即执行；补 `systemctl cat snaplink-sverp` 现场核查项（security 优先验证计划 #1）。
- **验证步骤**：`grep -n "s3cret\|demo-secret"` 服务器 systemd 单元零命中；默认凭据 + 生产标记
  → 启动失败（单测）。

### F7 [Low] — BFF 登出不撤销 access token（仅 refresh + OP 会话）

- **条款/等级**：RFC 7009 §2.1（access token 撤销可选）；OIDC RP-Initiated Logout（会话语义）。
- **证据（Verified）**：`snaplink.ts logout()` → `postRevoke(refresh_token)` + `postLogout`；access
  token 保持至 1h TTL。
- **影响**：登出后 1h 内，被窃取的 access token 仍可调 userinfo/API（ERP 侧各路由各自校验）。
- **纠正行为**：接受残余风险（短 TTL + 后端会话校验已兜底）或把 access token 加入 revoke 调用
  （snaplink 支持全 token 撤销）；记入残余风险清单。
- **验证步骤**：e2e——登出后重放 access token 断言 401（当前预期 200，需决策）。

### F8 [Info] — DPoP/mTLS 存在但全链路未启用；RS 侧 typ 门禁已闭环

- **证据（Verified）**：DPoP opt-in per-request（captureSenderConstraint），sverp-web 未发 DPoP 头；
  mTLS 需 clientCertExtractor 接线。RS 侧 `checkAccessTokenTyp`（RFC 9068 §4）已强制 at+jwt——
  防 ID token/SET/logout token 误路由。
- **影响**：无——bearer 模型 + LAN 信任是明确选择；DPoP 列为触发项（暴露面扩大时）。
- **纠正行为**：不接（与 5 不接清单"HTTPS+JWKS 触发门控"同思路）；文档化触发条件。

### F9 [Info] — 已验证的正向控制（记录，不构成 findings）

1. **Oracle 收敛**：token 端点所有失败统一 `invalid_grant`（token_authcode.go），错误分类 400/401/
   403 语义明确（governance oracle-safe 同款）。
2. **刷新令牌超集防护**：轮换 + 全族 kill + velocity cap + 绝对寿命（RFC 9700 §4.13 之上）。
3. **discovery 与真实路由一致性**：minimal 收窄（surface.go）、sso-server 由 wiring 推导；
   RFC 8414 alias 与 OIDC discovery 同 handler 同缓存。
4. **RFC 9207 双向**：AS 签发 `iss`、RP 校验 `iss`（service.ts）且校验发生在 code 交换**之前**。
5. **幂等 token 请求**：Idempotency-Key 快速路径（缓存命中在 client 认证之后，错 secret 重放
   不会命中缓存 200）。
6. **DPoP code 绑定**（RFC 9449 §10）：签发时绑定 jkt，交换时不匹配 → invalid_grant。
7. **BFF 侧**：state 常量时间比较 + 流单次消费（consumeAuthorization）、PKCE verifier 不进
   浏览器、刷新并发去重（refreshes Map）、登出时 refresh revoke + 会话删除先行。

---

## 4. 优先级一致性测试、声明不支持项、认证证据

### 4.1 优先级一致性测试（与里程碑绑定）

| # | 测试 | 绑定 | 现状 |
|---|---|---|---|
| P1 | **id_token 验证单测/e2e**（伪造签名/错 aud/错 iss/过期 → 拒绝） | 新增（F1 修复验收） | 缺口 |
| P2 | **密钥轮换收敛演练**：rotate 后 JWKS 双 kid 重叠 → 各 RS 切新 kid → 旧 kid 摘除；重启后无手工同步 | 1.2.1 验收追加 | 缺口（F2） |
| P3 | **部署面 discovery 抓取核对**：`/.well-known/openid-configuration` 字段 vs 实际挂载路由 vs OpenAPI | 1.2.2/L2 checklist | 需服务器执行 |
| P4 | **负向矩阵**：plain PKCE（strict 语义）、redirect_uri 未注册、code 重放二次交换、过期 refresh、已撤销 token 重启后仍被拒（security F2 验收） | 1.2.1/1.2.2/3.3（test-platform.sh negative） | 部分缺口 |
| P5 | **租户映射负向**：未绑定 client 的 token 写 governance → 拒绝；vault 跨 tenant 403 | 3.2 验收追加（F5） | 缺口 |
| P6 | **BFF 登出语义**：登出后 access token 行为断言（决策后固化） | 3.3 | 缺口 |
| P7 | **RS 负向**：错 iss/错 aud/HS256 token → 401（governance 已有部分，补全矩阵入 test-platform.sh） | 3.3 | Partial |

### 4.2 声明不支持（declared unsupported / 未接线，按部署面）

| 能力 | 状态 | 证据 |
|---|---|---|
| CIBA（backchannel） | minimal 隐藏 + gate off（featureGatesForEdition CIBA:false） | surface.go hiddenMetadataEndpoints；app.go |
| CAEP/SSF（RFC 8417 SET 推送） | gate off；client Attributes 有 `caep_receiver_endpoint` 字段但部署未启用 | app.go CAEP:false；kickoff 3.1 暂缓同意 |
| PAR（RFC 9126） | minimal 隐藏；sso-server 有实现但部署未启用 | surface.go；handle_par.go |
| Dynamic Client Registration（RFC 7591） | minimal 隐藏；console 有 DCR 界面（sso-server 面） | surface.go；console register_panel.dart |
| Device flow（RFC 8628） | minimal 隐藏 + `TestPrototypeRejectsClientCredentialsGrant` 同型拒绝；sso-server 挂载但部署未用 | server_routes.go:184 |
| SAML 2.0 SP | 非 stock 接线（需 fork 注册 handler） | build_http.go:372-398 |
| SCIM 2.0 | sso-server `/api/v1/scim/*` 权限门禁挂载；minimal 无；部署未用 | build_http.go:331 |
| WebAuthn/passkey | sso-server stock 有；minimal 无（authenticators 仅 password+OP session）；部署未用 | build_http.go:293 |
| 集中式 secrets/Vault/KMS 签名 | WithEd25519ExternalSigner 等存在；部署未用 | infrastructure/kms/ |
| JARM（签名响应） | 仅 signer 接线时接受；部署未用 | renderAuthCodeResponse |

### 4.3 认证证据（certification）

- **不宣称任何 OIDF 认证**：仓库无 OIDF 测试套件结果、无 conformance profile 声明；本评审仅
  基于内部测试套件（rootcov 流测试、minimal flow/surface、oauthwire fuzz、governance 契约对拍、
  sverpweb 单测/e2e）与静态读码。**剩余认证证据**：若未来需要 OIDC Core OP/RP 认证，前置 =
  F1 修复（RP 侧）、部署面 strict profile 显式化（F4）、发现文档与路由一致性自动化核对
  （P3）——OIDF conformance 套件本身不在本仓库范围内。

---

## 5. 与 kickoff/其他评审的裁决对齐

| 议题 | 本评审结论 | 对齐 |
|---|---|---|
| 1.2.1 密钥持久化 | 同意为 P0 根因；**验收必须追加** JWKS 双 kid 重叠收敛 + 吊销集持久化（security F2） | 对齐 devops C2 / security F2 / db C2 |
| 1.2.2 L2 前置 | 同意；追加"部署 profile 显式化（strict on/off 决策）"为 checklist 项 | 对齐 security F3/F8 |
| 1.2.3 console 审计页 | 无协议障碍；governance 读 API 契约已验（TESTING_GUIDE §4） | 对齐 |
| 3.2 scope 收窄 | 同意；注意 sso-admin 的 admin:* + 审计读面收窄后，**BFF 换 token 路径不受影响**（sverp-audit 持有 audit:event:write 即可） | 对齐 |
| 4.2 密钥轮换 | 同意；AUTH_PRESIGN_SECRET（vault 重启使在途 presign 失效）与 IM 口令补入清单 | 对齐 security F9 |
| 不接清单 | 全部维持；本评审新增不接项 = DPoP/mTLS 启用（触发条件=暴露面扩大） | 对齐 |
