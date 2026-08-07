证据已收到，完成归一（零 LLM 分析成本，事实原样保留）：

## 证据归一：`05-current-state.md`（第二阶段证据——平台当前状态，部署/实现后）

**provenance**：2026-08-06 部署/实现后实测（8 轮实现工作实录，192.168.123.52 / svmisdev / Debian 12），Verified 实测为主、Proposed 标注遗留。

**要点核对（供下游 aggregate/kickoff 引用）**：

- **部署现状**：8 服务 systemctl 全 active（snaplink-sverp :13015/16、console :13017、sverpweb :13014、governance :13018 dev-auth 关闭、vault :13019、aero-id :13020/21、aero-im :13022、nats :13023）；PG15/Redis/pgvector/NATS 基础设施齐备。
- **M0–M2 达成（第一轮 gate 阻塞项 B1–B5 已闭环）**：tsc 31→0、forwarding 8 用例、permanent 列 + 毒丸隔离 + 逐事件 receipts、erp_contract_test.go 5 测试、sverp-audit 服务账号 + schema/source 注册、审计 L2 E2E 全通（质检单 → outbox → 适配器 → 账本 actor=admin 无重复）、token 自动轮换（exp 缓存/提前刷新/401 重试）、集成测试页三服务全通过、IM 质检通知（M5 最小版）、test-platform.sh status 25/audit 8/e2e 8 全绿。
- **组合资产**：sso-admin / sverp-audit / sverp-vault 服务账号（最小权限待核查）、sverp-web client（OIDC 授权码 + PKCE）、admin/tester 双账号。
- **遗留清单（§4，第二阶段评估的处置对象）**：
  - M3 遗留：console 审计页未接 governance 账本（Flutter 侧改动）；
  - 密钥轮换：snaplink 重启即轮换（rotation.enabled=false 仍轮换）→ 长期 HTTPS+JWKS 动态；
  - M5 扩展：通知仅质检创建 1 个触发点；
  - vault 生产数据：ERP 媒体仍本地 SQLite（懒迁移+回填+双读禁双写未做）；
  - aero-id 组织投影未接（H2 降级中）；
  - 静态 token 残留（AUDIT_APP_TOKEN，OAuth 优先可清理）；
  - 安全：keys.txt 0600、DEMO_PASSWORD 死配置（N2 待办）、console 曾 inactive 一次；
  - 测试账号密码在服务器 accounts.txt，生产应轮换。

**下游衔接**：evidence 阶段 5/5 归一完成。本文件是 kickoff 的**当前事实基线**——第一阶段路线图 M0–M5 中 M0/M1/M2 已落地，评估须基于此重排剩余路线图（A）、逐条处置 §4 遗留（B）、核查组合纪律与最小权限（C）、给出运营建议（D）；证据 01/04 中已被实现推翻的旧论断（如"设置 env 即激活"）不得再作为现状引用。
