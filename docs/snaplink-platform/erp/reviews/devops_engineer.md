DevOps 评审完成，交付物已写入 `docs/snaplink-platform/erp/reviews/devops_engineer.md`。本次评审**实机核验**了生产主机 192.168.123.52（ssh + curl）与两仓库源码/部署资产。

## 核心结论（按严重度）

**Critical**
- **C1 生产二进制来自脏树**：主机 `sso-server v0.0.0-…-82ceb07374ce+dirty`；sverpweb-stable 仓库 `.git` 是空目录 → 无法复现/溯源任何在跑二进制
- **C2 门禁红且零 CI**：复跑 `pnpm typecheck` 失败（31 个 TS 错误，复现 QA F1）；release 全手动 tar/scp/切链接，无审批留痕

**High**
- **H1 L2 激活的部署面四件套全缺**（P0 在部署层被阻塞）：① 主机无 governance 实例；② `audit-provisioner` desired-state 无 sverp-web source/schema；③ governance 认证要求 OIDC claims + `audit:event:write`（裸 Bearer 不识别）；④ ingest 路径/字段映射不兼容（`/api/v1/audit/events` vs `/api/v1/events:batch` + `ValidateBasic` 必填清单）。仅设 `AUDIT_APP_URL` → 全批 400 → outbox failed 积压
- **H2 备份已过期**：主机无 crontab，数据备份止于 07-29（WAL 持续写至 08-04），且为运行中热拷 WAL/SHM，无一致性保证、无恢复演练
- **H3 构建不可复现**：`@snaplink/sso-client` 是仓库外 `file:` 依赖（lock 只记路径不记内容）

**Medium**：无迁移框架（schemaVersion 硬编码 1）、无周期 drainer、ERP 无 `/metrics`/告警（platform-status.sh 只是路径错误的示例）、密钥明文落盘无轮换、发布/回滚无 preflight 自动化。

## 做对的（Verified）
不可变 release + `current` 原子切换 + `shared` 数据/env 分离（回滚无损模型真实成立）；systemd 单元与仓库 `deploy/` 资产逐字节一致；三服务探针全通；outbox 状态机与 L1 现状吻合。

## 阻塞生产声明的关键待决策
① governance 实例与 URL；② sverp-web 注册；③ 凭据方案（OIDC client_credentials）；④ ingest 路径/映射（契约先行）；⑤ 租户映射；⑥ 备份自动化+RTO/RPO；⑦ git+CI+manifest；⑧ 监控接线；⑨ 发布脚本/保留策略；⑩ 旧 sverpweb 下线标准。

本评审为咨询性分析，不构成发布批准。
