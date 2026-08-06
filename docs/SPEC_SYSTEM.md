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
