# AI 软件工程组织规范（Agent Software Engineering Organization SOP）

> **定位**：把一个成熟软件公司的产品经理/业务专家/设计师/架构师/DBA/
> 前后端/测试/安全/DevOps/运维/技术负责人全部固化为 Agent 能力。
> 每个流程节点的规范包含 9 要素：**职责 / 输入 / 输出 / 检查规则 /
> 禁止事项 / 最佳实践 / 失败案例 / 自动检测脚本 / Review Checklist**。
>
> 机器可读规范：`agent-engineering/*.yaml`（`pi-batch org check` 校验
> 完整性）。自动检测列全部映射到仓库真实命令——规范可执行，不是文档。

## 0. 全流程节点总览

```
0 Orchestrator 总控（CTO+PM：任务识别/Agent 调度/上下文/技术债）
   │
1 Requirement 需求分析 → 2 Product 产品设计 → 3 UX/UI 体验设计
   │
4 Domain 业务建模 → 5 Architecture 架构设计 → 6 Data 数据设计
   │
7 API 接口设计 → 8 Planning 任务拆解（DAG）
   │
9 Development 开发实现（受规范约束的编码）
   │
10 Code Review → 11 Security → 12 Testing → 13 Performance
   │
14 Release 发布 → 15 Operation 运维 → 16 Evolution 演化（闭环到 0）
```

## 1. 各节点规范摘要

### 0. Orchestrator（研发总控）

- **职责**：任务识别（Bug/小需求/功能/重构/架构/迁移）、Agent 调度决策、
  上下文管理、决策记录、技术债、控制循环
- **检查规则**：任务类型分类（`classify`）；影响分析先行（`impact`）；
  工作等级路由（`profile` L0-L3）
- **自动检测**：`pi-batch classify`、`pi-batch impact --task`、
  `pi-batch profile`、`pi-batch capabilities trust`
- **Review Checklist**：□ 类型判定正确？□ 影响面已分析？□ 工作等级
  与流程匹配？□ 决策已记录（adr）？

### 1. Requirement（需求分析）

- **职责**：自然语言 → Requirement Specification（目标/角色/场景/验收）
- **检查规则**：8 维完整性（角色/主流程/数据源/权限/异常/验收/技术栈）；
  目标对齐
- **自动检测**：`pi-batch assess`、`pi-batch atomize`、`pi-batch reflect
  quick --task`（完整性维度）、`pi-batch impact`
- **失败案例**：把"增加审批"直接转代码 → 缺角色/异常/验收维度
- **Review Checklist**：□ 角色齐全？□ 异常流程设计？□ Given/When/Then
  验收标准？

### 2. Product（产品设计）

- **职责**：用户故事（As a/I want/So that）、流程设计（User Journey/
  State Machine）、功能拆分（Epic/Feature/Story/Task）、业务规则
- **检查规则**：产品化分级（L0-L3 克制处方）；推演链问题标记"待确认"
- **自动检测**：`pi-batch assess`（product 段）、`pi-batch profile`
  （goal_model 护栏）
- **失败案例**：L0 小工具被产品化结构污染（克制原则）
- **Review Checklist**：□ 用户故事完整？□ 状态机覆盖异常？□ 隐含需求
  已标记待确认？

### 3. UX/UI Designer（体验设计）

- **职责**：用户研究（频率/场景/痛点）、信息架构（层级/优先级）、页面
  布局（Grid/Spacing/Alignment/Hierarchy）、交互设计（Hover/Loading/
  Empty/Error/Success/Confirm）、Design System（Color/Typography/
  Component/Accessibility）
- **检查规则**：8pt 间距 token；12 列布局；语义色；禁止随机圆角/裸数字
  偏移；删除必须确认+原因+权限+审计
- **自动检测**：`scripts/check-ui-spec.py`、`scripts/check-ui-geometry.py`、
  `ui-specs/` 规范资产、`pi-batch ui-geometry`、`pi-batch reflect`（UX 维度）
- **失败案例**：AI 生成页面无几何美感（魔法间距/随机圆角/默认蓝）
- **Review Checklist**：□ 间距走 token？□ 无裸数字偏移？□ 空态/加载/
  错误反馈齐全？□ 删除类操作有确认+审计？

### 4. Domain（业务建模）

- **职责**：DDD（Entity/Value Object/Aggregate/Domain Service/Domain
  Event）、Bounded Context 划分、业务规则
- **检查规则**：领域边界不混（HR 的 Performance/Attendance 不混）；
  状态机完整性
- **自动检测**：`backend-specs/ddd.md`、`domain-modeling.md`、
  `pi-batch assess`（域判定）、`pi-batch profile`（hard triggers）
- **失败案例**：把绩效/考勤/工资揉进一个 Employee 模型
- **Review Checklist**：□ Bounded Context 清晰？□ Aggregate 边界正确？
  □ 业务规则可测试？

### 5. Architecture（架构设计）

- **职责**：架构模式选择（Monolith/Modular/Microservice/Event Driven）、
  SOLID、Clean Architecture、设计模式识别
- **检查规则**：依赖方向（Presentation→Application→Domain←Infrastructure）；
  不重复造已有系统（aero-id/snaplink/audit 复用）；决策必须 ADR
- **自动检测**：`scripts/check-backend-quality.py`（依赖方向）、
  `pi-batch graph extract`（循环）、`pi-batch adr find`（决策缓存）、
  `backend-specs/architecture-constitution.md`
- **失败案例**：重复造认证系统；微服务过度拆分
- **Review Checklist**：□ 依赖方向正确？□ 无模块级循环？□ 复用已有
  服务而非重造？□ 决策已入 ADR？

### 6. Data（数据设计）

- **职责**：ER 建模/范式/反范式、索引设计（查询频率/组合/覆盖）、事务
  （ACID/隔离/死锁/回滚）、迁移（向后兼容/脚本/回滚计划）
- **检查规则**：编码前必须输出 Persistence Design 报告；金额不用浮点；
  危险 DDL 必须 migration
- **自动检测**：`scripts/check-backend-quality.py`（DDL/浮点金额）、
  `backend-specs/persistence-modeling.md`（强制关卡）
- **失败案例**：生产库直接 ALTER；浮点存金额
- **Review Checklist**：□ 索引覆盖查询？□ 迁移有回滚？□ 金额类型正确？
  □ 审计字段齐全？

### 7. API（接口设计）

- **职责**：REST/GraphQL/RPC 契约、版本、幂等、分页、错误模型、权限
- **检查规则**：幂等键；错误码规范；版本策略；公共契约变更需评审
- **自动检测**：`backend-specs/`、`pi-batch impact`（API 受影响面）、
  `pi-batch profile`（跨项目硬触发器）
- **Review Checklist**：□ 幂等？□ 分页？□ 错误模型？□ 版本策略？

### 8. Planning（任务拆解）

- **职责**：设计 → DAG（DB→Backend→Frontend→Test）
- **检查规则**：拓扑依赖；关键路径；波次调度
- **自动检测**：`pi-batch graph`（依赖图）、`pbatch/waves.py`
  （plan_waves/parallel_safe）
- **Review Checklist**：□ DAG 无环？□ 依赖正确？□ 波次合理？

### 9. Developer（开发实现）

- **职责**：遵守规范编码（OOP/SOLID/DDD/DI/AOP/Repository）
- **检查规则**：函数 <50 行、复杂度 <10（本仓库 ≤15）、参数 <5、类
  <300 行、方法 <20；禁止 God Class/Function、Duplicate、Hard Code
- **自动检测**：`python quality.py`、`scripts/check-backend-quality.py`、
  `scripts/check-frontend-quality.py`、`pi-batch refactor --file`
- **失败案例**：967 行 cli.py god-file（本仓库教训）
- **Review Checklist**：□ 无新 god 文件？□ 复杂度达标？□ 依赖方向正确？
  □ 直接 new 依赖？□ 重复逻辑？

### 10. Code Review（代码审查）

- **职责**：架构/职责/依赖/质量审查 + 重构建议
- **检查规则**：职责是否正确；依赖方向；复杂度/耦合/重复
- **自动检测**：`pi-batch refactor`（职责/模式信号）、`pi-batch reflect
  architecture --code`、`prompts/critic.md`、`prompts/code_architecture_reviewer.md`
- **失败案例**：1000 行 Service 无人拆
- **Review Checklist**：□ SRP？□ ISP？□ DIP？□ 重复代码？

### 11. Security（安全审查）

- **职责**：OWASP（注入/XSS/CSRF/SSRF/Broken Access）、身份
  （OAuth/JWT/Session）、授权（RBAC/ABAC）
- **检查规则**：敏感域（支付/审计/生产）必须安全设计；权限矩阵
- **自动检测**：`pi-batch reflect security --task`、`pi-batch impact`
  （security 面）、`backend-specs/production-readiness.md`
- **Review Checklist**：□ 越权？□ 注入？□ 敏感数据？□ 权限扩大？

### 12. Testing（测试验证）

- **职责**：单元（领域规则）/集成（API/DB/Queue）/E2E（用户流程）
- **检查规则**：验收覆盖；未执行测试不得声称通过
- **自动检测**：`pi-batch eval`（规则回归）、`pytest tests/`、
  `scripts/check-completion-report.py`（禁止伪造通过）、
  `pi-batch nversion`（非确定性）
- **Review Checklist**：□ 验收标准有测试？□ 边界/负面/失败路径？
  □ 完成报告诚实？

### 13. Performance（性能优化）

- **职责**：后端（N+1/Cache/Queue/并发）、数据库（慢 SQL/索引/锁）、
  前端（Bundle/Lazy/Virtual List）
- **检查规则**：N+1；循环 await；大导出异步
- **自动检测**：`scripts/check-frontend-quality.py --strict`（N+1）、
  `pi-batch impact`（performance 面）、`pi-batch reflect`（性能维度）
- **Review Checklist**：□ N+1？□ 慢查询？□ 大文件异步？□ 缓存策略？

### 14. Release（发布）

- **职责**：Docker/K8s/CI-CD、Migration、Rollback、蓝绿
- **检查规则**：迁移安全；灰度；版本兼容；回滚就绪
- **自动检测**：`pi-batch profile`（rollback_required）、`pi-batch
  recovery`（回滚 vs 前向修复）、`pi-batch devices`（部署执行）
- **Review Checklist**：□ 迁移可回滚？□ 版本兼容？□ 回滚演练？

### 15. Operation（运维）

- **职责**：Log/Metric/Trace/Alert、SRE、Incident Response、RCA
- **检查规则**：可观测性节点；失联演练；Kill Switch
- **自动检测**：`pi-batch health`、`pi-batch metrics`、`pi-batch
  devices halt`、`pi-batch runner --drill-skip-heartbeats`、`pi-batch events`
- **Review Checklist**：□ 可观测？□ 演练过？□ 事故有 RCA？

### 16. Evolution（演化优化）

- **职责**：分析 Bug/性能/技术债/反馈 → Refactor Plan/Architecture
  Update/Knowledge Update
- **检查规则**：规则晋升（shadow→promote）；经验沉淀（ADR/facts）
- **自动检测**：`pi-batch retro --actions`、`pi-batch learn
  draft/shadow/promote`、`pi-batch reflect`、`pi-batch advance`、
  `pi-batch adr new`、`pi-batch project facts`
- **Review Checklist**：□ 重复问题已升规则？□ 决策已入 ADR？□ 记忆已更新？

## 2. 规范文件结构（agent-engineering/<id>.yaml）

每个节点一个 YAML，9 要素齐全（`pi-batch org check` 强制校验）：

```yaml
id: ux_designer            # 节点 id
name: UX/UI Designer Agent # 角色名
role: 设计师               # 中文角色
stage: 3                   # 流程序号（0-16，必须连续）
parent: product            # 上游节点
responsibilities: [...]    # 职责
inputs: [...]              # 输入
outputs: [...]             # 输出（契约）
check_rules: [...]         # 检查规则（可执行判据）
forbidden: [...]           # 禁止事项
best_practices: [...]      # 最佳实践
failure_cases: [...]       # 失败案例（真实教训）
auto_checks: [...]         # 自动检测脚本（仓库真实命令）
review_checklist: [...]    # Review Checklist
```

## 3. 使用方法

```bash
pi-batch org check          # 校验全部节点规范完整性（9 要素 + 顺序 + 命令存在）
pi-batch org show [NODE]    # 查看节点规范（JSON）
pi-batch org prompt NODE    # 岗位说明书 → 角色注入片段（喂给角色 prompt）
pi-batch org gate NODE --task "需求" --code DIR [--file F]
                            # 节点门禁：auto_checks 注入参数后执行
                            # （{task}/{code}/{file} 占位符；缺参 → skipped；
                            #  reflect 低分 → attention 不阻塞；门禁类失败 → gate FAIL）
pi-batch org flow           # 全流程节点图
pi-batch org run --task "需求" [--code DIR] [--level L0-L3]
                            # 流程串联：impact 分流 → 相关节点 gate 顺序
                            # 执行 → 汇总（L0/L1 轻量，L2/L3 全评审）
```

每个节点另配 `role_template`（prompts/ 角色模板）；`org check` 校验
parent 依赖与模板存在性。

- 节点规范 = Agent 的"岗位说明书"（`org prompt` 生成注入片段）
- auto_checks = 该节点的质量门禁（`org gate` 可执行化；缺参不伪装通过）
- review_checklist = 该节点的 Review Gate 标准（gate VERDICT 的依据）
