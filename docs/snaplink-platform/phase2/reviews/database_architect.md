数据库架构评审已完成，交付物写入 `docs/snaplink-platform/phase2/reviews/database-architect-review.md`（182 行，7 大节）。

## 评审摘要

**核实方式**：6 个仓库 pinned HEAD 静态读取（snaplink `d19838d8`、sverpweb `abd8801`、governance `476d083`、vault `c0ce6db`、aero-id `9a4ec42`、aero-im `53b0ced`）+ grep 扫描；服务器侧配置不可见（标记 Partial）。

**Store inventory（8 存储，热/持久判定）**：业务存储全部 durable-by-write（SQLite WAL/PG），**四处热暴露**：① governance `state.json` 无 fsync、零备份、单节点；② snaplink 签名密钥纯进程内存（每次重启新生成）；③ stock 默认仍热/不安全（sso-server `config.yaml` identity/oauth/audit=memory、audit-api dev-auth 默认 true——部署层已覆盖）；④ console localStorage 审计环。**Stock wiring 判定**：ERP 侧（sverpweb→governance L2 路径）是**唯一全接线**；vault relay 默认关、aero-im relay 默认关、aero-id 双交付路径仍在（inline publish 吞错）、sso-server **完全未接** governance 连接器（`auditgovernance` 仅被 `cmd/snaplink-billing` import）——kickoff 3.1 的"未验证缺口"应升级为"**已验证未接线**"。

**关键发现**：
- **C1（Critical）**：审计真源无备份+无 fsync+无恢复演练——L2 宣布的硬前置（4.3 优先级正确，补充 fsync 细节）。
- **C2（Critical）**："重启即轮换密钥"是 **stock 能力缺口非配置错误**——`SigningConfig` 无密钥文件选项，`rotation.enabled=false` 只关调度循环。1.2.1 需要新代码（load-or-generate key file），且密钥文件必须进 4.3 备份范围与 systemd `ReadWritePaths`；修正 4.3 表格"sqlite 含密钥"的表述（密钥不在 sqlite 里）。
- **H1**：outbox 保证 delivery at-least-once，但 **enqueue 与业务提交非原子**（quality create 两事务）——崩溃窗口丢审计事实，需补偿扫描器。
- **H2**：全库 `synchronous=NORMAL`——审计库应 FULL。
- **H3**：stock 二进制层未 fail-closed（L2 checklist 应加二进制级门禁）。
- **M1-M5**：备份零脚本、无保留策略（outbox published 行永不清理）、静态 token 回退、console 审计环、迁移碎片化。
- **L1（比 kickoff 更乐观）**：`DEMO_PASSWORD` 代码侧已死（HEAD 零引用），仅剩 .env 清理 + 校验，无代码改动。

**迁移序列**：Step0 备份先行（1.2.5 硬前置）→ 1.2.1 密钥持久化（首启 kid 变更一次的可接受窗口）→ 1.2.2 L2 前置 → 备份自动化 → 1.2.5 双读+回填+切流（禁双写，回滚=回拨开关）→ 1.2.3 console 读侧 → H1/H2/M2 硬化 → M5/1.2.7。每步含验证查询、回滚路径与数据完整性检查。

**未知项**：容量包络未测量（BenchmarkIngest 遗留）、retention 实际值未验证、恢复演练全部缺失、服务器侧配置不可见。
