DevOps 评审已完成，交付物写入 `docs/snaplink-platform/phase2/reviews/devops-engineer-review.md`。

## 评审摘要

**关键前提核实**：本仓库 **不存在 `ops/deploy/` 目录**（find 确认零文件）——评审指令指定的部署资产路径缺失。部署证据全部来自服务器实录文档与证据 05；`examples/snaplink-platform/suite-template/compose/*.yaml` 已确认为示例（实际部署是 systemd 而非 compose），未作为可部署证据使用。kickoff 正文已与 `/tmp/kickoff-final.md` 核对一致（仅差 wrapper 预验证头）。

**1. 受支持部署路径**：单主机 192.168.123.52、8 systemd 服务 + nats（:13013–:13023）、capistrano 风格 current/shared、加固单元 + GOMEMLIMIT 有界（Verified 做对项）。版本化资产清单：本仓只有示例脚本；单元/nginx/test-platform.sh/sync-sso-key.sh 全部只在服务器。

**2. 流水线表**：11 个阶段（源码门禁→CI→构建产物→部署→配置/密钥→探针→迁移→备份→发布/回滚→监控→轮换语义），每项含证据/缺口/建议门禁/owner。

**3. Findings（按严重度）**：
- **Critical C1**：部署资产零版本化（`ops/deploy/` 缺失）——不可复现/审计/回滚
- **Critical C2**：snaplink 重启即轮换密钥（rotation.enabled=false 无效）——阻塞 L2 宣布
- **Critical C3**：备份零脚本，governance 审计真源单节点无备份
- **High H1–H5**：secrets 明文未轮换（keys.txt / dev 级 PG 口令）、平台 7 仓无 CI、发布全手动无审批留痕（旧 dist 部署事故）、无监控/告警 + console 无自愈、DEMO_PASSWORD/AUDIT_APP_TOKEN 残留
- **Medium M1–M4**：迁移体系不统一（audit_outbox 隐式迁移无版本戳）、无 staging、探针不完整、测试资产未版本化
- **Low**：HTTPS 豁免合理但触发条件需文档化；做对项延续（加固单元、幂等安全网、current/shared 回滚模型）

**4. 发布/回滚 runbook**：preflight（三套件+备份+配置 diff+sha256 校验）→ 依赖链序发布（sverpweb 最后）→ 逐步探针 → post-deploy（e2e + drainer pending=0 + 密钥指纹）→ 显式轮换流程 → 回滚切 previous。**明确约束：1.2.1 落地前禁止非必要重启**。

**5. 阻塞项 B1–B8**：密钥轮换语义、备份体系、secrets 轮换、L2 前置收尾、部署资产版本化、CI 自动化、监控接线、console 审计页。核心裁定：**B1–B4 未落地前不得宣布 L2；B2 未落地前不得启动 vault 迁移**。

第一轮 devops 评审的 C1/C2/H2 修复状态已核对：git init + tsc 0 + governance 实例为部分达成（Verified），备份/CI 自动化/审批留痕/密钥轮换仍开放。本评审为咨询性分析，不构成发布批准。
