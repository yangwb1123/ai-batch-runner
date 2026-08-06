Security review complete → `docs/snaplink-platform/erp/reviews/security_engineer.md`.

## 本次实际执行（非转述）

- **`bunx tsc --noEmit`**：31 错，含 `request-middleware.ts:116/131/139` 三处（TS2345/TS2353），与 testing_reviewer 精确吻合
- **运行时复现 ×3**（bun 脚本挂载真实 app + contracts）：
  1. `withActor` 误用 → fallback 事件 `operation.actorUserId === null` + 顶层被摊入 3 个契约外键（S1，治理端键集校验会拒收）
  2. **5 个无 cookie POST → 5 条 outbox 行**（S4 未认证写放大，零限流零保留）
  3. **demo 模式身份伪造 → `demo:root` + `["*"]` 全量管理员**（S2 完整认证绕过）
- **静态走查**：CSRF 覆盖（29/29 mutating 路由全防护）、SQL 全参数化、文件路径三重穿越防护、会话 AES-256-GCM+AAD、PKCE/state 时序比较、safeReturnTo —— 均确认到位

## Findings（4 High / 5 Medium / 3 Low / 1 Info）

| # | Sev | 要点 |
|---|---|---|
| S1 | High | fallback 审计事件 actor 恒 null + 载荷契约污染（L2 发布阻断，gate 已 FAIL 同向）|
| S2 | High | demo 模式伪造授权码 → 任意访客全量管理员（已复现）|
| S3 | High | 全链路明文 HTTP + `.env` 活体密钥（client secret/会话密钥/死配置 DEMO_PASSWORD）→ 会话劫持 |
| S4 | Medium | 未认证 POST 写放大 outbox（已复现）+ 无保留策略 → 磁盘/治理洪水 |
| S5 | Medium | L2 激活即 100% 拒收（9 必填字段缺失）+ 4xx 与 5xx 同等待遇 → 永久 failed 积压 |
| S6 | Medium | Idempotency-Key 只采集不执行 → 业务重放无防护 |
| S7 | Medium | 256MiB body + 50MB 上传 arrayBuffer + 无限流 → 内存 DoS |
| S8 | Medium | L2 审计传输与 Bearer token 明文过网 |
| S9–S12 | Low/Info | trace 头投毒（有界）、/health 泄露、trigger 丢触发、保留/备份双侧皆空 |

**正面控制**：认证/授权/CSRF/注入/文件安全五面扎实（会话加密、PKCE、BFF、29/29 CSRF、全参数化 SQL、路径三重校验）——可作修复基线。

**与 gate 一致**：当前状态不可启用 L2、不具备 audit-ready 条件；每项均附先红后绿的回归测试规格与 P0–P2 验证计划。咨询性意见，不构成发布批准。
