SRE 评审完成，交付物写入 `docs/snaplink-platform/phase2/reviews/sre-engineer-review.md`。

## 评审摘要

**核实方式**：5 个仓库 pinned HEAD 静态读码（sverpweb `abd8801`、snaplink `d19838d8`、governance `476d083`、vault `c0ce6db`、console `0c21dca`）+ 服务器运维资产实测副本 `/tmp/deploy-build/test-platform.sh`（151 行）+ platform-247.sh 全文 + grep 扫描（定时器/metrics/healthz）。服务器侧 systemd 单元与 journald 不可见（标记 Unknown）。

**1. 服务/依赖图**：单主机全栈（8 服务+PG/Redis/NATS 一台机器）；检测面 = 人工跑 test-platform.sh，无告警、无采集；恢复面 = 重启即密钥轮换（1.2.1 根因）+ 零备份。**部署平台无任何 SLO/RTO/RPO**——governance 仓 SLO_ALERTS.md 是另一架构（Kafka/ClickHouse）的草稿且自声明"不得宣称"，不继承任何数值。

**2. 就绪度表**：9 行（BFF/snaplink/governance/vault/im/console/outbox），每项含信号、依赖、失效行为（Verified）、告警（全部"无"）、runbook（全部"无"或人工）。

**3. Findings（按严重度）**：
- **Critical C1（本评审独有）**：检测层整体缺失——BFF 唯一探针是**假阳性**：脚本探 `:13014/healthz` 落入 SPA catch-all 返回 200 index.html（app.ts:197-205），API 半死时 status 全绿；console 零探针；snaplink minimal 无健康端点（flow_test 断言 /metrics 404）；governance /metrics 现成但从未采集；platform-247 无健康阶段。
- **Critical C2**：审计真源无 fsync+无备份+无恢复演练（与 devops C3/DB C1 合并）——RPO 事实上 ∞。
- **Critical C3**：密钥轮换缺陷使**回滚/重启这一恢复动作本身成为故障注入**（恢复面视角）。
- **High H1（本评审独有）**：outbox **无进程内周期 drainer**（client.ts:285 明示不开定时器）——重试被"下一次业务写"门控；"drainer 60s"文档承诺无代码支撑。
- **High H2**：无 SLO/RTO/RPO 定义；需 owner 决策（BFF 可用性 ≥99.5%、审计交付 p99 ≤60s、RPO=备份频率、RTO≤1h 演练实测）。
- **High H3**：outbox 无界增长（每笔变更请求落一行、永不清理）。
- **High H4**：配置 fail-open + 无过载防护 + presign expires 客户端可控 + AUTH_PRESIGN_SECRET 空=重启即失效（security F4/F9 的 SRE 面）。
- **Medium M1-M4**：ops 资产未版本化（恢复工具不可复现）、console 无自愈、容量包络缺失、7×24 巡检无健康阶段。
- **Low**：脚本 9 单元与 kickoff"8 服务"计数漂移（`sverpweb` 疑旧实例残留）；governance /metrics 一行 curl 可采集（性价比最高观测增量）。

**4. 故障演练**：outage（停 governance）、saturation（写放大+磁盘满）、bad rollout（旧 dist 复演）、stale state（密钥轮换+吊销）、restore（账本恢复）五场景表，含注入/期望/恢复验证/当前状态（4/5 不满足）。

**5. 阻塞项与残余风险**：B1–B5（1.2.1+吊销持久化、备份+fsync+演练、检测面、fail-closed、SLO 决策）；回滚触发条件 5 条；监控缺口 9 项现状→目标；残余风险 = 单主机单点、LAN 信任模型、stock 默认值、容量/保留未知、恢复依赖未版本化资产。

**裁决意见（Proposed）**：withstand 基础良好（优雅停机/有界超时/幂等安全网/逐事件 receipts/有界退避）；**detect 层（C1）与 recover 层（C2/H1）缺失构成 L2 硬前置**——探针修正 + drainer + 告警 + 备份/恢复演练落地前不建议宣布 L2；1.2.1 验收必须并入吊销持久化断言（security F2）；"drainer 60s"须代码落地或降级为人工 flush runbook。
