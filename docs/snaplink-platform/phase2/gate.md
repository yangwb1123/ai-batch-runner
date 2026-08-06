# 终裁：第二阶段评估

## 裁决

**VERDICT: FAIL - 剩余路线图主体定序与克制纪律成立，但对抗审查提出的 4 项实质性阻塞项未并入路线图（F1 现网越权、F2 吊销复活、QA Critical 验收基线损坏、database C2/H1/H2/H3 硬化缺口），且 1.2.1/1.2.2 验收标准不完整，L2 宣布前置清单在按当前路线图执行后仍无法真实达成。**

---

## 标准逐条核验

### 标准 1：定序有证据支撑且可执行（验收标准明确）— 部分满足

**认可（Verified 支撑，多评审交叉确认）**：
- 1.2.1 密钥轮换语义作为根因置顶——devops C2、database C2、security F2 均确认"重启即轮换"是根因，方向一致；
- 1.2.5 vault 迁移后置于备份体系之后——devops 裁定"B2 未落地前不得启动 vault 迁移"、database 迁移序列 Step0 备份先行，与 kickoff 完全对齐；
- 1.2.4 pi-batch 嵌入提前（git init 前置已达成）成本极低，合理。

**不满足处（验收标准不完整，直接导致 FAIL 的证据链）**：
- **1.2.1 验收缺吊销持久性断言**：security F2（Verified，`ed25519_jwt_issuer.go:67,101-104` 吊销集纯进程内存）指出密钥持久化修复本身会引入"已吊销 token 重启后复活"。当前验收只写"重启后三套件全绿且无需手工同步"——修复达标后仍带着安全退化。须补：吊销 token → restart → 该 token 仍被拒（snaplink 单测 + sverpweb 登出后重启旧 refresh 换新失败 e2e）。
- **1.2.1 成本与实现路径低估**：database C2（Verified）确认这是 stock 能力缺口非配置错误（`SigningConfig` 无密钥文件选项），需新代码 load-or-generate + `keys.signing.revocation_backend: sqlite` 接线；"半天级"未含吊销存储接线与密钥文件入 4.3 备份范围、systemd `ReadWritePaths`。
- **1.2.2 验收清单缺已实证的现网越权项**：security F1（Verified 读码：`routes/integrations.ts` 8 条路由全部仅 requireSession）——tester 可读全量审计 Payload + 读写媒体 + 伪造 IM 通知。**当前 L2 checklist 全部勾选后 L2 依然不成立**（security 明确"F1/F2 落地前不建议宣布 L2"）。F1 未并入 1.2.3 前置或独立 P0，是路线图最大缺口。
- **验收基线本身损坏**：QA Critical——e2e 账本查询窗口硬编码 2026-08-06→08-07，**8 月 7 日零点起 e2e 必然失败**，1.2.1/1.2.2 验收依赖的三套件与 4.1 的 7×24 巡检信号全部失真。kickoff 无处置项。

### 标准 2：新发现问题逐条有处置 — 满足

kickoff §2.1–2.5 对证据 05 §4 发现的处置完整：密钥轮换决策矩阵（根因修复→timer 兜底→HTTPS+JWKS 触发门控）、DEMO_PASSWORD 清除 + fail-closed、AUDIT_APP_TOKEN 移除 + OAuth fail-closed、console 审计页归 1.2.3、inactive 复盘四项动作——每条均有验收断言。**注意**：F3/F8 与 1.2.2 方向一致但范围需扩（默认凭据 `alice/s3cret` 拒绝 + `IDENTITY_MODE` 严格枚举 + 生产标记拒 demo 模式）；F4 的 presign TTL 钳制与覆盖 409 须并入 1.2.5 验收（3.2 现只提跨 tenant 403）。

### 标准 3：组合纪律与克制原则 — 保持

11+4 项不接清单全部维持并复核无翻案信号；AuditSink 暂缓（security 同意）、HTTPS/JWKS、集中 secrets、aero-im 跨主机凭据均带触发条件；QA 克制（不新增 chaos/load 套件仅登记触发条件）、database 不引入多节点复制，与 kickoff 一致。**满足**。

### 标准 4：对抗审查阻塞项已处理或带证据驳回 — 不满足（FAIL 主因）

| 阻塞项 | 级别 | 证据 | 路线图处置状态 |
|---|---|---|---|
| F1 集成面 RBAC 绕过（tester→审计读/媒体写/IM 伪造） | High | Verified 读码 + 利用链完整 | **未处理**（security：1.2.3/3.2 均未覆盖） |
| F2 吊销集重启复活（1.2.1 修复引入的退化） | High | Verified 读码 | **未处理**（1.2.1 验收缺断言） |
| QA Critical：e2e 日期窗口硬编码 8/6→8/7 | Critical | 静态追踪脚本 | **未处理** |
| DB C1：state.json 无 fsync | Critical | Verified | 部分（4.3 已列最高优先级，补 fsync 细节即可） |
| DB H1：outbox enqueue 与业务提交非原子（崩溃丢审计事实） | High | Verified 两事务 | **未处理**（无补偿扫描器） |
| DB H2：审计库 synchronous=NORMAL | High | Verified | **未处理** |
| DB H3：stock 二进制层未 fail-closed | High | Verified | **未处理**（1.2.2 只到 sverpweb 配置层） |
| DevOps B5：部署资产零版本化（ops/deploy/ 缺失） | Critical | find 实证 | 部分（3.3 仅 test-platform.sh 入 suite，systemd/nginx 资产未覆盖） |
| DevOps B6：7 仓 CI 自动化 | High | Verified | 部分（1.2.4 仅 sverpweb 侧） |

以上全部为 Verified 证据支撑，无一项可带证据驳回；F1/F2 是 security 明确"必须"级，QA Critical 是时效性硬故障。

---

## 修订路径（FAIL 后的最小处理清单）

1. **新增/并入 P0 硬化项**：F1 集成路由权限矩阵（`requirePermission` 细分或统一 `system:settings:read` + presign key 前缀校验）→ 并入 1.2.3 前置或独立里程碑，验收 = tester 全路由 403 矩阵入 test-platform.sh negative；
2. **1.2.1 扩验收**：吊销→重启→仍拒；revocation_backend 接线；密钥文件入 4.3 备份范围与 systemd 权限；成本修正为"1-2 天含吊销存储"；
3. **1.2.2 扩验收**：默认凭据拒绝 + IDENTITY_MODE 严格枚举 + 生产拒 demo；L2 checklist 增加 F1 修复项与 stock 二进制级门禁（H3）；
4. **QA Critical**：e2e 日期窗口改相对时间/运行时间参数化，列为 1.2.2 前置修复；
5. **DB H1/H2**：补偿扫描器 + 审计库 synchronous=FULL 并入硬化序列（低成本，防 L2 审计承诺破洞）；
6. **4.2/4.3/2.1 补充**：F9（AUTH_PRESIGN_SECRET、IM 口令入清单）、F5（备份加密 + 恢复演练含解密验证）、F6（触发条件补"LAN 段出现不受信主机"）、F4（presign TTL 钳制入 1.2.5 验收）。

已满足项无需返工：定序逻辑、§2 逐条处置、克制纪律、4.3/4.4/3.2 主体结构。修订后重新提交终裁。
