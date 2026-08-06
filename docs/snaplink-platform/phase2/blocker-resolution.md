# 第二阶段 gate FAIL 阻塞项处置（2026-08-06 第 10 轮）

> 来源：phase2-pipeline gate 终裁（VERDICT: FAIL，4 项实质阻塞）+ 本轮实现。
> 处置状态：F1/QA Critical **已修复并验证**；F2/DB H1 **已评估并定处置**。

## 阻塞项处置对照

| 阻塞项 | 级别 | 处置 | 状态 |
|---|---|---|---|
| **F1 集成面 RBAC 绕过**：integrations 路由仅 requireSession，tester 可读审计/写媒体/伪造 IM | High | 8 条路由全部加 `requirePermission("system:settings:read")`（commit 待收录） | ✅ **已修复**，实测 tester 403 / admin 200 |
| **QA Critical：e2e 日期窗口硬编码 8/6→8/7** | Critical | test-platform.sh 改为动态日期（`date -u` 今天 00:00 → 明天） | ✅ **已修复**，三套件 25/8/8 全绿 |
| **F2 吊销集重启复活**：snaplink 吊销集纯进程内存 | High | 实测：当前"重启即轮换密钥"使旧 token 全部失效（隐式全量吊销），**暂掩盖该问题**；但修复密钥持久化时必须同步 `revocation_backend: sqlite`（snaplink 代码缺口，SigningConfig 无密钥文件选项——需 load-or-generate + 吊销存储接线，列 A-1 硬化项） | 📋 已定处置（A-1 前置条件），未实现（snaplink 代码级改动，触发条件=密钥持久化修复时） |
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
