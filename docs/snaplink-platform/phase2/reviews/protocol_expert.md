评审完成，交付物写入 `docs/snaplink-platform/phase2/reviews/identity_protocol_expert.md`。

## 评审摘要

**核实方式**：6 仓 pinned HEAD 静态读码 + `docs/openapi.yaml`（18219 行）对照；逐条核对 handler（login/token/userinfo/discovery/jwks/endsession/revoke/introspect）、两 edition 的 discovery 元数据、OpenAPI 与测试套件（rootcov 流测试、sso-minimal flow/surface、oauthwire fuzz、sverpweb 单测/e2e）。

**协议面判定**：已部署 = sso-server stock 二进制 + minimal 配置，**profile 实为 OAuth 2.0 而非 OAuth 2.1**（`config.yaml:74 oauth_21_strict_mode: false`，minimal 原型强制 true）——部署面 plain PKCE/http redirect 均可存活。

**Findings（9 项）**：

- **F1 [High] OIDC Core §3.1.3.7 违反**：sverpweb BFF 收到 id_token **零验证**（无签名/iss/aud/exp/nonce 校验，apps/api 无任何 JWT 库），只存储不校验——与请求 `openid` scope 的宣称不符。
- **F2 [High] 密钥轮换语义**：重启即换 key（kid 实测 `9WQoSnawsm8→gwvKK02OO-g`）与 RS 缓存/静态注入的收敛窗口——1.2.1 验收必须追加"JWKS 双 kid 重叠期 + 各 RS 切 kid 收敛"断言。
- **F3 [Medium] 全链路明文 HTTP**：Basic client secret、refresh token、code 均 LAN 明文；PKCE/state/轮换杀族已缓解 code 与旧 refresh 泄露，残余 = 活跃 refresh + client secret。
- **F4 [Medium] strict profile 未显式决策**（两 edition 行为分叉）；**F5 [Medium] token 无 tenant claim**，各 RS 自建 client→tenant 映射，vault 的 `ten` claim 回退是读 snaplink 永不签发 claim 的死路径；**F6 [Medium] stock 默认配置安全基线**（默认凭据/dev-auth）；F7–F9 为 Low/Info。

**正向控制已验证**：token 端点 oracle 收敛（全失败统一 invalid_grant）、刷新令牌轮换+全族 kill+velocity cap（超 RFC 9700 §4.13）、RFC 9207 双向、RFC 9068 typ 门禁 + kid 唯一、discovery 与真实路由一致（minimal 收窄 + sso-server wiring 推导）、DPoP code 绑定、BFF state 常量时间比较+流单次消费。

**不宣称 OIDF 认证**；声明不支持项清单（CIBA/CAEP/PAR/DCR/device/SAML SP/SCIM/WebAuthn/JARM 在部署面均未启用）已列；P1–P7 优先级测试与里程碑绑定，其中 P1（id_token 验证）为新增门禁缺口。
