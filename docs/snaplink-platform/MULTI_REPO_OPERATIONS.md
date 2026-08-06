# 多项目联动操作模型（独立产品 + 组合验证）

> 事实前提：6 个独立 GitHub 仓库（snaplink / snaplink-admin /
> snaplink-audit-governance / aero-id / aero-vault / aero-im），各自独立
> CI 门禁与 AGENTS.md。它们是**独立可部署产品**（见 COMBINATION_MODEL.md），
> 通过标准协议组合；ai-batch-runner 的 campaign / worktree 隔离都是
> **仓库内**能力——跨仓库没有原子操作。联动靠“组合仓库 + 看板 + 轮巡”，
> 不靠自动合并。

## 1. 三层模型

```
┌─ 产品层（6 仓库自治）─────────────────────────────────────────┐
│  每仓库自带 pi-batch.py：campaign / advance / rolling / meta    │
│  默认单仓库变更；跨仓库契约按需（先三层判断，再动双方契约）      │
│  决策日志与 state.jsonl 是本地事实源                            │
└───────────────┬───────────────────────────────────────────────┘
                │ 镜像 + 版本窗口 + 事件/API 契约
                ▼
┌─ 组合层（snaplink-suite 仓库，不承载业务代码）─────────────────┐
│  compose 多拓扑（minimal/identity/vault/full）                  │
│  compatibility.yaml 兼容矩阵（suite-check.py 门禁）             │
│  E2E 边界场景清单 + 契约测试 + 部署文档                         │
└───────────────┬───────────────────────────────────────────────┘
                │ 冒烟/验收结果反馈到各产品
                ▼
┌─ 验证层（跨仓库集成，唯一权威）────────────────────────────────┐
│  compose 集成栈：登录→操作→审计事件→查询 端到端断言             │
│  （借鉴 audit-governance deploy/docker-compose.verify.yml 模式）│
└────────────────────────────────────────────────────────────────┘
```

**为什么这样分**：AI 流水线擅长“仓库内”的发现-设计-实现-门禁闭环；
跨仓库的**行为一致性**（A 服务发的审计事件 B 服务能按 schema 解析、
token 撤销后 C 服务会话失效）只能靠契约冻结 + 集成栈验证。AI 不能替代
集成栈，但能产出并维护契约与用例。

## 2. 联动机制（结果如何流动）

| 环节 | 机制 | 命令/产物 |
|---|---|---|
| 单仓库变更 | 默认只改当前仓库（三层判断：单项目/可选集成/组合行为） | campaign / advance 单仓库运行 |
| 契约草案 | 产品仓库各自产出提案 → suite 汇总为事件目录/矩阵变更 | 人工批准后提升 |
| 组合验证 | suite 仓库：compatibility.yaml + compose 拓扑 + E2E | `suite-check.py` + E2E 清单 |
| 状态汇聚 | 看板脚本聚合 7 仓库状态（6 产品 + suite） | `platform-status.sh` |
| 无人值守 | 轮巡脚本逐仓库滚动分析 + 失败补跑 | `platform-247.sh` |
| 集成验证 | 组合栈端到端用例（CI 触发） | compose + 验收断言 |
| 事故沉淀 | gate FAIL / 集成失败 → RCA → learn → 规则草案 | `learn` 子命令 → docs/rules/drafts/ |
| 契约变更 | 兼容性分析 → 版本化 → 旧客户端兼容窗口 | suite 矩阵更新 → 各仓库按需升级 |

## 3. 执行序列（4 周节奏）

```
第 0 周（半天，人工）:
  □ 同步 ai-batch-runner 到 6 仓库（薄壳 + pbatch/ + pi-batch.yaml + 新版验证器）
  □ 每仓库跑 `python pi-batch.py check`（环境自检）
  □ 初始化 snaplink-suite 组合仓库（suite-template 模板）
  □ 主仓库跑 `platform-status.sh` 建立基线看板

第 1 周（依赖先行）:
  □ audit-governance 仓库跑生产化 campaign（全平台依赖它，先于一切接入）
     campaign --modules "internal,cmd,migrations,deploy" --jobs 4 --max-directions 2
  □ 审计接入契约草案（各产品仓库产出提案 + suite 汇总矩阵）
  □ 人工批准契约 v1：audit 事件注册表 + scope 矩阵 + 接入设计

第 2 周（核心接入）:
  □ 契约 v1 下发到 snaplink + console（docs/agent-context/）
  □ snaplink campaign：审计接入（L2 适配器）+ 平台 scope 注册表落地
  □ console campaign：统一审计时间线页（前端流水线 + uispacing/uicolor/uistyle + uicheck）
  □ suite 组合栈 v0：snaplink + audit-governance + console 三服务端到端

第 3-4 周（铺开 + 硬化）:
  □ 契约下发 aero-id / aero-vault / aero-im（aero-id 已有 connector 模板，改造即可）
  □ 各仓库 campaign 实现接入 + outbox 表迁移
  □ suite 组合栈扩展到 6 服务；审计事件类型清单逐项断言
  □ 每周轮巡 + gate FAIL 复盘 → learn 沉淀规则（如“写操作无审计事件 = FAIL”）

持续:
  □ 每日 platform-247.sh 轮巡；每周人工裁决 gate FAIL 与 learn 草案
  □ 契约版本管理：变更走契约流水线 v2，逐仓库升级，verify 栈先绿
```

## 4. 每仓库"平台上下文包"清单（docs/agent-context/ 模板）

```
docs/agent-context/
├── platform-contract.md        # 当前契约版本 + 本服务职责边界 + 依赖方向
├── audit-events-v1.md          # 批准后的平台审计事件注册表（本服务相关节）
├── scope-matrix-v1.md          # 本服务最小 scope 集
├── service-map.md              # 6 服务 API 地址/端口/健康检查（开发环境）
└── integration-gates.md        # 集成验证用例清单（verify 栈断言）
```

`context` 子命令按路径 glob + 关键词自动加载；缺失平台契约时显式警告，
防 agent 在无契约状态下臆造事件类型。

## 5. 工具边界与纪律（诚实清单）

- **没有**跨仓库 campaign / 跨仓库原子提交；跨仓库代码修改 = 各仓库独立
  PR + 人工评审 + suite 组合栈把关；默认只改当前仓库
- **不做**自动冲突合并；worktree 隔离只在一个仓库内生效
- **组合仓库不搬业务代码**：suite 只承载 compose/兼容矩阵/E2E/契约测试
- **AI 产物是提案**：契约、事件类型、scope 矩阵必须人工批准后才进
  docs/agent-context/；集成断言必须由组合栈实际执行过才算 Verified
- 每仓库硬门禁（make ci / cli.py check / AGENTS.md 不变量）优先于一切
- 生产凭据未验收项（aero-im 已有标注纪律）不得被流水线“修绿”外推
