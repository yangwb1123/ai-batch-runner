# 多项目联动操作模型（6 仓库 = 1 个平台）

> 事实前提：6 个独立 GitHub 仓库（snaplink / snaplink-admin /
> snaplink-audit-governance / aero-id / aero-vault / aero-im），各自独立
> CI 门禁与 AGENTS.md。ai-batch-runner 的 campaign / worktree 隔离都是
> **仓库内**能力——跨仓库没有原子操作。因此联动靠"契约 + 看板 + 轮巡"
> 三层模型，不靠自动合并。

## 1. 三层模型

```
┌─ 契约层（主仓库 ai-batch-runner = 平台编排仓库）───────────────┐
│  跨仓库契约流水线 → 契约草案 → 人工批准 → 版本化平台契约包        │
│  docs/snaplink-platform/  +  examples/snaplink-platform/       │
└───────────────┬───────────────────────────────────────────────┘
                │ 批准后复制进每仓库 docs/agent-context/（版本号）
                ▼
┌─ 实施层（6 仓库自治）─────────────────────────────────────────┐
│  每仓库自带 pi-batch.py：campaign / advance / rolling / meta    │
│  上下文路由自动加载平台契约；决策日志与 state.jsonl 是本地事实源  │
└───────────────┬───────────────────────────────────────────────┘
                │ 镜像 + compose 集成栈 + 契约验收用例
                ▼
┌─ 验证层（跨仓库集成，唯一权威）────────────────────────────────┐
│  平台 verify 栈（借鉴 audit-governance deploy/docker-compose.verify.yml│
│  outbox→relay→redpanda→ledger→ClickHouse→MinIO Object Lock 模式）│
│  = 全 6 服务镜像一栈拉起 + 端到端用例（登录→操作→审计事件→查询）   │
└────────────────────────────────────────────────────────────────┘
```

**为什么这样分**：AI 流水线擅长"仓库内"的发现-设计-实现-门禁闭环；跨仓库的
**行为一致性**（A 服务发的审计事件 B 服务能按 schema 解析、token 撤销后 C 服务
会话失效）只能靠契约冻结 + 集成栈验证。AI 不能替代集成栈，但能产出并维护
契约与用例。

## 2. 联动机制（结果如何流动）

| 环节 | 机制 | 命令/产物 |
|---|---|---|
| 契约产出 | 主仓库流水线（含对抗审查 + VERDICT 门） | `audit-onboarding-pipeline.yaml` → 3 个契约草案 + gate |
| 人工批准 | 按证据标准核对，盖章版本号 | 契约包 `<契约名>-v1.md` 进入发布 |
| 契约下发 | 复制进每仓库 `docs/agent-context/`（context 路由自动加载） | 每仓库 `context` 子命令验证加载 |
| 实施发现 | 每仓库 campaign 模块扫描（契约作为上下文） | `campaign --dry-run` → state.jsonl + SUMMARY.md |
| 状态汇聚 | 看板脚本聚合 6 仓库状态 | `platform-status.sh`（见下） |
| 无人值守 | 轮巡脚本逐仓库滚动分析 + 失败补跑 | `platform-247.sh` |
| 集成验证 | 平台 verify 栈端到端用例（CI 触发） | compose + 验收用例（契约验收标准转成脚本断言） |
| 事故沉淀 | gate FAIL / 集成失败 → RCA → learn → 规则草案 | `learn` 子命令 → docs/rules/drafts/ |
| 变更传播 | 契约 v2 → 各仓库 rolling 分析发现"对接版本漂移" | 滚动分析方向天然带出，campaign 实现 |

## 3. 执行序列（4 周节奏）

```
第 0 周（半天，人工）:
  □ 同步 ai-batch-runner 到 6 仓库（薄壳 + pbatch/ + pi-batch.yaml + 新版验证器）
  □ 每仓库跑 `python pi-batch.py check`（环境自检）
  □ 主仓库跑 `platform-status.sh` 建立基线看板

第 1 周（依赖先行）:
  □ audit-governance 仓库跑生产化 campaign（全平台依赖它，先于一切接入）
     campaign --modules "internal,cmd,migrations,deploy" --jobs 4 --max-directions 2
  □ 主仓库跑跨仓库审计契约流水线（audit-onboarding-pipeline.yaml）
  □ 人工批准契约 v1：audit 事件注册表 + scope 矩阵 + 接入设计

第 2 周（核心接入）:
  □ 契约 v1 下发到 snaplink + console
  □ snaplink campaign：审计接入 + 平台 scope 注册表落地
  □ console campaign：统一审计时间线页（前端流水线 + uispacing/uicolor/uistyle + uicheck）
  □ 平台 verify 栈 v0：snaplink + audit-governance + console 三服务端到端

第 3-4 周（铺开 + 硬化）:
  □ 契约下发 aero-id / aero-vault / aero-im（aero-id 已有 connector 模板，改造即可）
  □ 各仓库 campaign 实现接入 + outbox 表迁移
  □ 平台 verify 栈扩展到 6 服务；审计事件类型清单逐项断言
  □ 每周轮巡 + gate FAIL 复盘 → learn 沉淀规则（如"写操作无审计事件 = FAIL"）

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
  PR + 人工评审 + verify 栈把关
- **不做**自动冲突合并；worktree 隔离只在一个仓库内生效
- **AI 产物是提案**：契约、事件类型、scope 矩阵必须人工批准后才进
  docs/agent-context/；集成断言必须由 verify 栈实际执行过才算 Verified
- 每仓库硬门禁（make ci / cli.py check / AGENTS.md 不变量）优先于一切
- 生产凭据未验收项（aero-im 已有标注纪律）不得被流水线"修绿"外推
