# 规范体系总览（Spec System Map）

本仓库是一套 **AI 软件工程决策操作系统**：把资深工程师经验编码为
分层规范 + 算法匹配 + 机器门禁 + 对抗审查 + 经验沉淀闭环。规范库很大，
但单次任务注入量始终有界（scale × page_type × risk 分级处方）。

## 1. 规范资产分层

| 层 | 目录 | 内容 |
|---|---|---|
| 前端视觉 | `ui-specs/` | spacing（8pt token）/ component / layout-patterns / anti-patterns / business-profiles×7（erp/cms/oa/dashboard/immersive/marketing/mobile） |
| 前端工程 | `ui-specs/engineering/` | async-data（防抖/竞态/幂等决策表）、form-table-state、interaction-patterns、error-recovery、architecture-budgets、defect-patterns（12 事故档案） |
| 后端架构 | `backend-specs/` | architecture / design-patterns（决策表+过度设计红线）/ domain-modeling / evolution / oop-and-di / ddd（分级）/ algorithms-data-structures / complexity-and-scale / persistence-modeling（强制关卡）/ production-readiness / agent-guardrails / architecture-constitution / testing / system-engineering / network-engineering |
| 产品思维 | `product-specs/` | product-thinking（L0-L3 分级+推演链）/ commercial-readiness（低成本预留vs高成本实现）/ open-source-readiness / completion-evidence（证据报告） |
| 项目知识 | `docs/agent-context/` | context-map.yaml（路径/关键词 → ADR/契约/安全基线路由） |

## 2. 规则注册表（算法消费）

| 注册表 | 域 | 轴 |
|---|---|---|
| `ui-specs/rules.yaml` | 前端 | scale（demo<standard<production）× page_type（form/table/detail/workbench/immersive/auth）× risk |
| `backend-specs/rules.yaml` | 后端 | 同上（use_case_types：crud/workflow/integration/query + 算法场景 search/sort/graph/schedule/cache/range）|

每条规则：`id / description / files / min_tier / page_types / risk /
required_when_risk / profiles`。`pi-batch rules --check` 校验 schema。

## 3. 决策链（一次任务如何被处理）

```
需求文本
 ├─ classify      判域：frontend_ui / backend / analysis / ...
 ├─ rules         算法匹配规则 manifest（scale×page_type×risk）+ LLM 双向校验
 │                 （REQUIRED 规则不可被 LLM 跳过；增补带 provenance）
 ├─ assess        需求评估：8 维完整性 / 规模 S-M-L / 产品化 L0-L3 /
 │                 工作流 L0-L3（建议可执行流水线文件）
 ├─ context       项目文档路由（路径 glob + 关键词 → ADR/契约）
 ├─ 流水线        plan（含强制设计关卡）→ implement（机器门禁+证据报告）
 │                 → meta 对抗审查（34+ 角色）→ VERDICT 裁决
 └─ 完成          completion_report（commands executed vs not_executed）
```

## 4. 机器门禁（可确定检查的绝不靠自觉）

| 门禁 | 检查 | 验证器 |
|---|---|---|
| 前端 UI 规范 | 魔法间距/硬编码颜色/inline style（单产物+项目级） | uispacing/uicolor/uistyle/uicheck |
| 前端工程 | console.log/any/不安全 html/空 catch/测试 skip（恒失败）；上帝文件/复杂度/useEffect 泄漏/循环 await（strict） | uiquality |
| 后端工程 | 浮点金额/SELECT */无租户查询/LIKE 全扫/危险 DDL/状态直接赋值/domain→infra 依赖（恒失败或 strict） | backendquality |
| 完成证据 | completion_report 结构/result 枚举/not_executed 带原因/禁伪造通过 | completion |
| 秘密扫描 | api key/token 等 | secretscan |
| 注册表 | 规则 schema/引用文件存在 | rules --check |
| 规则回归 | 25 个 eval 断言（classify/rules/assess 行为） | make eval / CI |

## 5. 角色库（meta 编排动态挑选，34+）

前端：ui_designer / ui_reviewer / visual_reviewer / frontend_engineer /
async_reviewer / code_architecture_reviewer
后端：backend_engineer / design_pattern_reviewer / algorithm_reviewer /
testing_reviewer / architecture_constitution_reviewer / architect / ...
产品：product_thinker
通用：security_engineer / qa_lead / pm / cto / ...

## 6. 经验沉淀闭环（Evolution Engineering）

```
事故/审查发现
 → pi-batch learn（--from-memory 可从消息记忆取失败）
 → docs/rules/drafts/*.yaml（统一 Rule Schema，人工批准门槛）
 → 并入域注册表 rules.yaml
 → 匹配器/门禁/审查角色下次自动消费
 → pi-batch eval 防回归（改关键词/规则破坏行为 → make ci 失败）
```

## 7. 克制原则（整个体系的纪律）

- 小 demo（L0/scale S）只加载视觉核心 2 条规则；生产级才全套
- 处方档 = min(关键词档, 规模档)——"企业登录页"不因"企业"二字全量上规则
- 无商业信号不设计 Billing；无开源意图不生成开源文档；无第二使用方不抽象
- 推演出的隐含需求是"待确认问题"，未经确认禁止实现
- 每次规则选择都有证据与 provenance，可审计

## 8. 快速入口

```bash
pi-batch check                      # 一键自检（quality+registry+eval）
pi-batch assess "需求文本"          # 需求评估（处方+产品化+工作流）
pi-batch rules "需求" --llm-json ...  # 规则匹配+双向校验
pi-batch context "任务" --paths ...   # 项目文档路由
pi-batch eval                       # 规则回归套件
pi-batch learn "失败描述"           # RCA→规则草案
```

## Design Intelligence 层（认知驱动设计，2026-08 新增）

`ui-specs/design-intelligence/` 把"产品设计大脑"沉淀为最高层规范——
组件驱动是代码执行，认知驱动才是产品设计：

| 规范 | 内容 | 最低档位 |
|---|---|---|
| 01-product-thinking | Who→Why→What→How、角色模型、JTBD、Attention Ranking、异常优先 | demo（必选） |
| 02-color-intelligence | 色彩智能：场景→心理→信息权重→色彩语言；企业/SaaS/AI/大屏/Dark 范式；Design Token；双编码 | standard（必选） |
| 03-information-priority | 信息优先级：视觉焦点、KPI 大数字、数据价值决定面积、3 秒规则、Visual Attention Score | demo（必选） |
| 04-cognitive-load | 认知负担：渐进式展示、错误预防、空状态教学、信任、心流 | standard |
| 05-emotional-design | 情绪设计：本能/行为/反思层、掌控感、完成感、品牌气质 | production |
| 06-data-expression | 数据表达：大数字/健康度/语义色图表/决策建议 | standard（workbench） |

配套执行门禁 `scripts/check-design-intelligence.py`（注册为
`designintelligence` 验证器）：语义色 token 化、KPI 强调、空状态教学、
状态双编码——确定性拦截"数据库展示"反模式。已在 snaplink-console
实测：183 → 0 违规（含真实修复 ProgressRing 裸 amber）。

设计链：需求 → Who/Why/What/How → 信息优先级 → 色彩 token →
认知负担 → 组件 → 代码（prompts/ui_designer、visual_reviewer、
frontend_engineer 已注入）。

## 后端设计智能层（业务闭环，2026-08 新增）

`backend-specs/design-intelligence/` 把"体验→模型→架构→运营"完整链沉淀为
后端最高层规范——前后端不是接口连接，是业务闭环：

| 规范 | 内容 | 最低档位 |
|---|---|---|
| 01-experience-driven-api | 体验驱动 API：场景卡→聚合上下文端点、交互模式→API 映射 | standard（必选） |
| 02-domain-lifecycle | 业务生命周期与状态机：管道模式、集中状态机、事件驱动 | standard（高风险必选） |
| 03-ui-driven-data-model | UI 驱动数据模型：页面需求→模型、派生三策略、审计历史、租户边界 | standard（必选） |
| 04-experience-architecture | 体验架构：体验预算反推 CQRS/缓存/异步/事件流 | production |
| 05-operation-intelligence | 运营智能：埋点先行、行为分析、A/B、假设回测 | production |
| 06-product-pipeline | 产品工程流水线：先考察再立项、循环验证、预判 3 年、Phase 0-9 | production |

执行门禁 `scripts/check-backend-experience.py`（注册为 `backendexperience`
验证器）：状态机纪律（散落状态字面量 vs 集中集合）、写操作审计信号、
长任务异步生命周期、搜索端点聚合上下文。

完整设计链（前后端统一）：
需求 → 场景/角色/JTBD → 体验原型 → 信息优先级+色彩智能 → 体验预算
→ 领域模型/状态机 → 数据模型 → 体验驱动 API → 前端组件 → 后端领域
→ 门禁（designintelligence + backendexperience）→ 埋点 → 假设回测。

## 哲学层与维护智能（2026-08 新增）

- `docs/ENGINEERING_PHILOSOPHY.md`：软件工程哲学总纲——软件是认知外化
  （SmartFunction）、人类六大缺口、维护的本质（模型-现实偏差/软件熵/
  真实性/审计即信任）、五个变化源、六层维护、AI 时代定位（Agent 补充
  人类的四件事）、Problem Solving Intelligence。
- `backend-specs/design-intelligence/07-maintenance-intelligence.md`：
  维护智能规范——六层维护（数据/业务模型/UX/架构/安全/知识）、
  数据易错来源（人类行为不确定性）、Maintenance Agent 职责、
  与工具机制映射（advance/learn/eval/context/campaign）。
- 执行门禁 `scripts/check-knowledge-freshness.py`（注册为 `knowledge`
  验证器）：模块文档缺失（知识维护）、ADR 缺失（决策记录）、
  关键金额/库存字段无审计信号（信任）。

体系全链（前端+后端+维护）：
需求 → 哲学定位（软件为何存在）→ 场景/角色/JTBD → 体验原型 →
信息优先级+色彩智能 → 体验预算 → 领域模型/状态机 → 数据模型 →
体验驱动 API → 前端组件 → 后端领域 → 门禁（designintelligence +
backendexperience + knowledge）→ 埋点 → 假设回测 → 维护演化。

## 分发、进化与系统分类学（2026-08 新增）

- `docs/ENGINEERING_PHILOSOPHY.md` 扩展三章：
  - **分发本质**：软件是"信息能量"——传播降低不确定性的能力与控制能力；
    物理（光传播能量）/信息论（降熵）/控制论（感知-判断-行动）/生物学
    （数字基因）四视角；网络效应与生态是设计的一部分；AI 时代
    "软件→能力"分发。
  - **进化闭环**：Agent 自我迭代 = 控制论反馈（Goal→Observe→Reason→
    Act→Feedback→Update）；四层学习数据（Observation/Outcome/Reasoning/
    Knowledge）；Error→Rule Pipeline；Reflection 复盘；Agent Governance
    规则等级（L0 不可违反 → L3 实验）。
  - **问题系统分类学**（哥德尔启发）：没有一个系统能解决所有问题——
    先判定 prompt 属于哪类系统（状态机/事件/实时/搜索/优化/知识/批量/
    自适应/协作/确定性），再选该类方法论。
- `classifier` 新增 `system_type` 维度（零 LLM 成本关键词判定，YAML 可配）：
  `pi-batch.py classify "订单审批流" --json` → `system_type: state-machine`。
- `backend-specs/design-intelligence/08-distribution-intelligence.md`：
  分发智能规范（传播路径设计/能力分发/分发质量工程保障）。

## UI 几何与业务契约资产（2026-08-07 新增）

| 资产 | 层级 | 机制 |
|---|---|---|
| `ui-specs/geometry.md` | 规范（demo 档必读） | `uigeometry` 验证器机械拦截 + `pi-batch ui-geometry` 子命令 |
| `ui-specs/business-ui-contract.md` | 规范（standard+ 页类型） | rules.yaml 自动注入；评审角色裁决依据 |
| `scripts/check-ui-geometry.py` | 门禁脚本 | 裸偏移/随机圆角/随机线宽 fail closed；sr-only/clip 豁免 |
| `tests/test_ui_geometry.py` | 回归 | 8 用例（拦截/豁免）随 make ci |
| evals/rules.yaml | 规则回归 | +3 用例（geometry 必含、business-ui 分级） |
