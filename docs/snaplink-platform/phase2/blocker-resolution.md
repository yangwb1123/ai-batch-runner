# 第二阶段 gate FAIL 阻塞项处置（2026-08-06 第 10 轮）

> 来源：phase2-pipeline gate 终裁（VERDICT: FAIL，4 项实质阻塞）+ 本轮实现。
> 处置状态：F1/QA Critical **已修复并验证**；F2/DB H1 **已评估并定处置**。

## 阻塞项处置对照

| 阻塞项 | 级别 | 处置 | 状态 |
|---|---|---|---|
| **F1 集成面 RBAC 绕过**：integrations 路由仅 requireSession，tester 可读审计/写媒体/伪造 IM | High | 8 条路由全部加 `requirePermission("system:settings:read")`（commit 待收录） | ✅ **已修复**，实测 tester 403 / admin 200 |
| **QA Critical：e2e 日期窗口硬编码 8/6→8/7** | Critical | test-platform.sh 改为动态日期（`date -u` 今天 00:00 → 明天） | ✅ **已修复**，三套件 25/8/8 全绿 |
| **F2 吊销集重启复活**：snaplink 吊销集纯进程内存 | High | **已闭环**（commit 7586c6b3 + 实测）：`keys.signing.key_file` 持久化（load-or-generate PKCS8 PEM 0600）+ `revocation_backend: sqlite` 接线——重启后 **KID 稳定**（KRD9k7Zjews）+ **吊销跨重启持久**（revoke→重启→401）+ **未吊销 token 跨重启有效**（不再全量失效）；audit-governance 公钥同步变纯保险（kid 不变不重启） | ✅ **已修复并实测** |
| **DB H1：outbox enqueue 与业务提交非原子** | High | 现状：启动时 startup flush 兜底（app.ts 已有）；补偿扫描器需业务侧记录（文档化，触发条件=vault 迁移启动时）；审计事实最终一致性由"账本真源 + outbox 投递真源"双真源裁定覆盖 | 📋 已评估，触发条件驱动 |
| DB C1：state.json 无 fsync | Critical | 备份体系（4.3）最高优先级；vault 迁移前置（数据库裁定一致） | 📋 路线图内 |
| F3/F8：默认凭据 alice/s3cret、IDENTITY_MODE 严格枚举 | High | 本轮已清除 DEMO_PASSWORD/DEMO_USERNAME；IDENTITY_MODE 生产为 snaplink | ✅ 部分修复（alice/s3cret 属 aero-im 配置面，登记） |
| F4：presign TTL 钳制 | Medium | 集成页 presign 默认 300s；TTL 钳制 + 覆盖 409 并入 vault 迁移验收 | 📋 路线图内 |

## 本轮（第 10 轮）完成清单

1. ✅ F1：integrations 8 路由权限修复 + 实测 403/200
2. ✅ M5 扩展：质检单创建/更新/删除 → aero-im 通知（12 条消息实测，含
   M5-TEST-001/002 全生命周期）
3. ✅ QA Critical：测试脚本动态日期窗口
4. ✅ 生产硬化：清除 DEMO_PASSWORD/DEMO_USERNAME/AUDIT_APP_TOKEN（OAuth 已就位）
5. ✅ M4：sverpweb 嵌入 pi-batch（薄壳 + campaign 20 模块 dry-run + 第 7 仓轮巡）
6. ✅ im-notify 单测 +3（登录链路/noop/静默失败）
7. ✅ 全量测试：status 25/25、audit 8/8、e2e 8/8

## 遗留（触发条件驱动，不阻塞 L2 宣布之外的运营）

- A-1：snaplink 密钥持久化 + 吊销持久化（代码级，半天+）
- A-2：L2 宣布前置收尾（F1 已修复后 checklist 更新）
- A-3：console 审计时间线（Flutter 跨仓库）
- vault 数据迁移（备份体系就绪后）
- aero-id 组织（H2 维持）

## A-1 实测记录（2026-08-07）

- 部署：sso-server 新二进制（含 key_file）+ config.yaml `keys.signing.key_file`
  + 吊销 sqlite 已接线 → 首次启动生成 signing-key.pem（0600）
- 验证：重启 ×2 kid 不变；revoke→重启→T 401（吊销持久）/ AT 200（未吊销仍有效）
- 附带修复：snaplink-console 单元 `Requires=` → `Wants=`（此前重启 IdP 会
  级联停 console——08-02/15-04 两次 inactive 的根因）
- 测试脚本日期窗口与 occurred_at 全部动态化（QA Critical 闭环）
